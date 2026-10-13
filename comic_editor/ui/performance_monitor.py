"""Opt-in instrumentation of an editor; detached reports never read artwork."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from functools import wraps
from pathlib import Path
import threading
import time
import weakref

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QEvent, QObject, QTimer, Signal, Qt, qVersion
from PySide6.QtWidgets import QApplication

from comic_editor.core.performance_monitor import PerformanceRecorder
from comic_editor.core.performance_log import PerformanceLogWriter
from comic_editor.core.performance_resources import ProcessResourceSampler
from comic_editor.core import settings as settings_module


_active_controller = None
_MISSING = object()
_monitor_scope = threading.local()


@dataclass(frozen=True)
class _MonitorToken:
    # No scheduler, snapshot, QWidget, scene or image references cross threads.
    controller: object
    generation: object
    epoch: int
    metadata: tuple

    def details(self):
        return dict(self.metadata)


class _LogJsonFacade:
    """Reversible log-module alias; every encoding operation stays stdlib."""
    def __init__(self, original):
        self._original = original
        self.dumps = original.dumps

    def __getattr__(self, name):
        return getattr(self._original, name)


def _value(value):
    return str(getattr(value, "value", value))[:160]


def _rect(rect):
    return list(rect.getRect()) if rect is not None else None


class PerformanceMonitorController(QObject):
    """No timer, sampling, logging or instrumentation runs until start()."""

    changed = Signal()

    def __init__(self, window, *, log_directory=None):
        super().__init__(window)
        self.window = window
        self.canvas = window.canvas
        self.recorder = PerformanceRecorder()
        self.log_directory = Path(log_directory) if log_directory else (
            settings_module.settings_path().parent / "performance-logs")
        self.last_log_path = None
        self.log_status = "Monitoring is off. No log is being written."
        self._writer = None
        self._writer_finalized = False
        self._resources = None
        self._patches = []
        self._capture_generation = None
        self._run_serial = 0
        self._owned_schedulers = ()
        self._owned_capture_adapters = ()
        self._owned_cache_backings = weakref.WeakKeyDictionary()
        self._owned_input_jobs = weakref.WeakKeyDictionary()
        self._owned_cage_jobs = weakref.WeakKeyDictionary()
        self._owned_monitor_objects = weakref.WeakKeyDictionary()
        self._cache_backings_lock = threading.RLock()
        self._connections = []
        self._counts = Counter()
        self._critical_paint_boundaries = set()
        self._critical_paint_epoch = None
        self._feedback_contact_serial = 0
        self._feedback_contact_wait = None
        self._active_feedback_paint = None
        self._input_receipt = None
        self._current_input_receipt = None
        self._input_receipt_serial = 0
        self._gate_input_receipts = weakref.WeakKeyDictionary()
        self._deferred_input_receipts = {}
        self._text_presentation_serial = 0
        self._text_presentation_wait = None
        self._editor_metrics = {}
        self._ui_thread = threading.get_ident()
        self._last_refresh = 0.0
        self._filter_installed = False
        self._disposed = False
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        # A deleted widget must not leave a sampler/writer alive.
        self.destroyed.connect(lambda *_: self._shutdown_workers())

    @property
    def enabled(self):
        return self.recorder.enabled

    def context(self):
        """Bounded metadata only: no serialization, geometry or image reads."""
        canvas = self.canvas
        chapter = canvas.chapter
        kind, identifier = canvas.selected_kind, canvas.selected_id
        entity = None
        if chapter is not None:
            entity = (chapter.layers if kind == "layer" else chapter.objects).get(identifier)
        modifiers = []
        ancestry = []
        if entity is not None and chapter is not None:
            current = entity
            seen = set()
            for _ in range(32):
                for mid in getattr(current, "modifier_ids", ())[:32]:
                    modifier = chapter.modifiers.get(mid)
                    modifiers.append({"id": mid, "type": type(modifier).__name__})
                parent = getattr(current, "parent_id", getattr(current, "parent_layer_id", ""))
                if not parent or parent in seen:
                    break
                seen.add(parent)
                current = chapter.layers.get(parent)
                if current is None:
                    break
                ancestry.append(parent)
        solo = list(canvas._solo_entities)
        return {
            "tool": _value(canvas.tool),
            "selection": {"kind": kind, "id": identifier,
                          "type": type(entity).__name__ if entity else None,
                          "count": len(canvas.selected_entities),
                          "ancestor_ids": ancestry,
                          "modifier_stack": modifiers[:64]},
            "solo": {"enabled": bool(solo), "count": len(solo),
                     "entities": [list(item) for item in sorted(solo)[:64]]},
            "chapter": None if chapter is None else {
                "id": chapter.chapter_id, "width": chapter.width, "height": chapter.height,
                "layers": len(chapter.layers), "objects": len(chapter.objects),
                "effects": len(chapter.modifiers), "masks": len(chapter.masks)},
            "camera": {"center": [canvas.center_x, canvas.center_y],
                       "scale": canvas.scale, "rotation": canvas.rotation,
                       "viewport": [canvas.width(), canvas.height()],
                       "device_pixel_ratio": canvas.devicePixelRatioF()},
            "renderer": type(canvas).__name__,
            "requested_renderer": canvas.settings.canvas_renderer,
            "active_mask_id": canvas.active_tone_mask_id,
            "gesture": {"drawing": canvas._drawing, "vector": canvas._vector_gesture_mode,
                        "cage": canvas._cage_session is not None,
                        "transform": canvas._transform_preview_quad is not None},
            "focus": self._focus_metrics(),
        }

    def _focus_metrics(self):
        # GUI-only bounded identity. Never inspect entered text, labels or
        # values; the background writer uses the detached cached dictionary.
        if threading.get_ident() != self._ui_thread:
            return {"widget_class": None, "canvas_or_descendant": False}
        widget = QApplication.focusWidget()
        return {"widget_class": str(widget.metaObject().className())[:64] if widget is not None else None,
                "canvas_or_descendant": bool(widget is not None and
                    (widget is self.canvas or self.canvas.isAncestorOf(widget)))}

    def _metrics(self):
        canvas = self.canvas
        jobs = canvas._effect_jobs
        gpu = getattr(canvas, "_gpu_pattern_renderer", None)
        data = {
            "runtime": {"pyside_version": pyside_version, "qt_version": qVersion(),
                        "qt_platform": QApplication.platformName()},
            "focus": self._focus_metrics(),
            "events": dict(self._counts),
            "scene": {"dirty_full": canvas._scene_dirty_full,
                      "dirty_rect": _rect(canvas._scene_dirty_widget),
                      "bytes": canvas._scene_cache.sizeInBytes(),
                      "preview_channel": getattr(canvas, "_effect_preview_channel", "canvas")},
            "cache": {"effect_entries": len(canvas._modifier_render_cache),
                      "effect_bytes": canvas._modifier_render_cache_bytes,
                      "source_entries": len(canvas._modifier_source_cache),
                      "source_bytes": canvas._modifier_source_cache_bytes,
                      "vector_index_entries": len(canvas._vector_spatial_indexes),
                      "compound_entries": len(canvas._compound_path_cache)},
            "effect_jobs": {"pending": len(jobs.pending), "running": jobs.running is not None,
                            "running_count": len(jobs.running_jobs), "worker_limit": jobs.worker_limit,
                            "bytes_in_flight": jobs.bytes_in_flight,
                            "retained_bytes": jobs.retained_bytes, "budget_bytes": jobs.budget,
                            "retained_budget_bytes": jobs.retained_budget,
                            "shared_retained_bytes": jobs.retained_shared_bytes,
                            "shared_entries": len(jobs._retained_shared),
                            "submitted": jobs.submitted, "completed": jobs.completed,
                            "discarded": jobs.discarded},
            "gpu_patterns": {"available": bool(gpu and gpu.available),
                             "reason": getattr(gpu, "reason", "not initialized"),
                             "source_uploads": getattr(gpu, "uploads", 0),
                             "target_uploads": getattr(gpu, "color_uploads", 0)},
            "legacy_input": canvas.performance_snapshot(),
            "cache_owner": "Legacy canvas pools; detached worker caches are not read by the GUI monitor.",
        }
        projection = getattr(canvas, "_document_projection", None)
        if projection is not None:
            # Read only the fixed scalar counters. Never retain configuration
            # keys, tile records, image handles, or future snapshot content.
            snapshot = projection.snapshot()
            data["projection"] = {
                name: snapshot[name] for name in (
                    "tiles", "active_tiles", "configurations", "bytes", "hits",
                    "renders", "incomplete", "evictions",
                ) if type(snapshot.get(name)) is int
            }
            data["projection"].update(
                enabled=bool(getattr(canvas, "_document_projection_enabled", False)),
                budget_bytes=projection.budget, tile_size=projection.tile_size,
                revision=projection.revision,
                frame_pending=bool(getattr(canvas, "_projection_frame_pending", False)),
                render_failed=bool(getattr(canvas, "_projection_render_error", None)),
                presented_revision=getattr(canvas, "_projection_presented_revision", -1),
            )
        presentation = getattr(canvas, "_document_presentation_stats", None)
        if presentation is not None:
            data["presentation"] = {
                "backend": str(presentation.backend)[:32],
                "tiles": presentation.tiles, "uploads": presentation.uploads,
                "texture_bytes": presentation.texture_bytes,
            }
        preparation = getattr(canvas, "_distort_preparation_cache", None)
        if preparation is not None:
            data["distort_preparation"] = {
                name: getattr(preparation, name)
                for name in ("bytes", "budget", "hits", "misses", "evictions", "entry_limit")
            }
            data["distort_preparation"]["entries"] = len(preparation._entries)
        if self._resources is not None:
            data["process"] = self._resources.sample()
        stores = getattr(canvas.tiles, "_tiles", {})
        data["raster_storage"] = {
            "owners": len(stores), "tiles": sum(len(tiles) for tiles in stores.values()),
            "selected_object_tiles": len(stores.get(canvas.selected_id, {})),
            "tile_size": canvas.tiles.tile_size,
        }
        autosave = getattr(self.window, "_autosave_jobs", None)
        if autosave is not None:
            data["autosave"] = {"running": autosave.running is not None,
                                "pending": len(autosave.pending), "submitted": autosave.submitted}
        return data

    def _details(self, name, args, kwargs):
        details = {"thread_id": threading.get_ident(),
                   "thread": "gui" if threading.get_ident() == self._ui_thread else "worker"}
        if name in {"set_tool", "_activate_tool", "_activate_named_tool"}:
            details["requested_tool"] = _value(args[0]) if args else _value(kwargs.get("tool"))
            details["previous_tool"] = _value(self.canvas.tool)
        elif name == "_apply":
            command = args[0] if args else kwargs.get("command")
            action = args[1] if len(args) > 1 else kwargs.get("action")
            details["history_action"] = str(action)[:16] if action is not None else None
            details["history_command_type"] = type(command).__name__ if command is not None else None
        elif name == "_publish_change_set":
            change = args[0] if args else kwargs.get("change")
            details["change_action"] = str(kwargs.get("action", "transient"))[:16]
            details["entity_changes"] = len(getattr(change, "entities", ()))
            details["resource_changes"] = len(getattr(change, "resources", ()))
            details["conservative"] = bool(getattr(change, "conservative", False))
        elif name == "set_selection":
            details["requested_selection"] = [_value(value) for value in args[:2]]
        elif name == "set_solo_entities":
            # Do not consume a generator supplied to the actual operation.
            if args and isinstance(args[0], (list, tuple, set)):
                details["requested_solo"] = [list(value) for value in list(args[0])[:64]]
        for value in args[:4]:
            if hasattr(value, "sizeInBytes") and hasattr(value, "width"):
                details["image"] = {"width": value.width(), "height": value.height(),
                                    "bytes": value.sizeInBytes()}
                break
        for value in args[:4]:
            identifier = getattr(value, "object_id", None) or getattr(value, "layer_id", None)
            if identifier:
                details["entity"] = {"id": identifier, "type": type(value).__name__,
                                     "modifier_ids": list(getattr(value, "modifier_ids", ()))[:32]}
                break
        if name == "_modifier_layer_signature" and args:
            details["entity"] = {"id": args[0], "type": "layer"}
        if name == "_render_scene_cache_rect" and args:
            details["dirty_widget_rect"] = _rect(args[0])
        if name == "_paint_document_projection":
            details["live_ink"] = bool(kwargs.get("live_ink", False))
        elif name == "_collect_document_projection":
            details["projection_phase"] = args[0] if args else kwargs.get("phase")
        elif name == "_render_document_tiles" and args and isinstance(args[0], (list, tuple)):
            details["requested_tiles"] = len(args[0])
        elif name == "_render_document_region" and len(args) >= 3:
            details.update(world_rect=_rect(args[0]), pixel_scale=args[1],
                           output_size=[args[2].width(), args[2].height()],
                           exact=bool(kwargs.get("exact", False)),
                           requested_world_rect=_rect(kwargs.get("requested")))
        if name in {"_draw_predictive_ink", "_draw_live_vector_gesture", "_render_document_region"}:
            details["projection_phase"] = getattr(
                self.canvas, "_show_on_top_phase" if name.startswith("_draw") else "_projection_capture_phase", None)
        if name == '_begin_stroke_impl':
            # A bounded subset of the native feedback gate, captured on GUI
            # receipt. Empty blockers are not a promise of prepared coverage:
            # transform/pixel-contract and detached preparation still validate.
            chapter = self.canvas.chapter
            obj = chapter.objects.get(self.canvas.selected_id) if chapter is not None else None
            blockers = []
            if obj is not None:
                entities = [('object', obj)]
                entities.extend(('ancestor', layer) for layer in
                    chapter.ancestor_layers(obj.parent_layer_id)[:64])
                for prefix, entity in entities:
                    for attribute, label in (('modifier_ids', 'modifiers'),
                            ('show_on_top', 'show_on_top'), ('opacity_mask', 'opacity_mask')):
                        if getattr(entity, attribute, None):
                            blocker = prefix + '.' + label
                            if blocker not in blockers:
                                blockers.append(blocker)
            details['feedback_known_blockers'] = blockers
        if name == "render_distort":
            modifier = args[2] if len(args) > 2 else kwargs.get("modifier")
            if modifier is not None:
                details["modifier"] = {"id": modifier.modifier_id,
                                       "type": modifier.modifier_type}
            details["pixel_scale"] = args[6] if len(args) > 6 else kwargs.get("pixel_scale", 1.)
        details["channel"] = getattr(self.canvas, "_effect_preview_channel", "canvas")
        return details

    def _current_scope(self):
        token = getattr(_monitor_scope, "value", None)
        if (isinstance(token, _MonitorToken) and token.controller() is self
                and token.generation is self._capture_generation and self.enabled
                and token.epoch == self.recorder.capture_epoch):
            return token
        return None

    @contextmanager
    def _owned_scope(self, details, *, generation=None, capture_epoch=None):
        previous = getattr(_monitor_scope, "value", _MISSING)
        token = details if isinstance(details, _MonitorToken) else _MonitorToken(
            weakref.ref(self), self._capture_generation if generation is None else generation,
            self.recorder.capture_epoch if capture_epoch is None else capture_epoch,
            tuple(details.items()))
        _monitor_scope.value = token
        try:
            yield token
        finally:
            if previous is _MISSING:
                del _monitor_scope.value
            else:
                _monitor_scope.value = previous

    def _scheduler_scope(self, args, kwargs):
        # This path runs on the worker: read only weak scheduler identity and
        # the submitted immutable demand. Never read canvas/context/selection.
        scheduler = args[0] if args else None
        role = next((role for reference, role in self._owned_schedulers
                     if reference() is scheduler), None)
        if role is None:
            return None
        demand = args[1] if len(args) > 1 else kwargs.get("demand")
        if demand is None:
            return None
        document = demand.snapshot.document
        return {"ownership": "monitored_editor", "scope": "detached_worker",
                "consumer": str(demand.consumer)[:32], "scheduler_role": role,
                "demand_serial": demand.serial, "document_revision": document.revision,
                "chapter_id": str(demand.snapshot.chapter.chapter_id)[:64],
                "live_preview": bool(document.live_preview), "record": bool(demand.record),
                "run_generation": self._run_serial}

    def _capture_scope(self, args, _kwargs):
        capture = args[0] if args else None
        if (threading.get_ident() != self._ui_thread or capture is None
                or capture.owner is not self.canvas):
            return None
        jobs = getattr(self.canvas, '_scene_consumers', None)
        job = getattr(jobs, 'active', None)
        if job is not None and job.capture is capture:
            cage = self._cage_consumer_scope((jobs, job), {})
            if cage is not None:
                return {**cage, 'scope': 'gui_cage_capture_slice'}
        details = {"ownership": "monitored_editor", "scope": "gui_capture_slice",
                   "document_revision": capture.document.revision,
                   "run_generation": self._run_serial}
        # Both adapters own captures from the same canvas. Infer the consumer
        # only when its existing capture actually matches this slice.
        for reference, role in self._owned_capture_adapters:
            adapter = reference()
            if adapter is not None and adapter.capture is capture:
                details.update(consumer=role, scheduler_role=role, demand_serial=adapter.serial)
                break
        return details

    def _cache_bind_scope(self, args, _kwargs):
        # Only this editor's ordinary GUI binding may attach an IO owner.
        if (threading.get_ident() != self._ui_thread or not args
                or args[0] is not getattr(self.window, 'disk_cache', None)):
            return None
        return {"ownership": "monitored_editor", "scope": "gui_cache_bind",
                "consumer": "canvas", "run_generation": self._run_serial}

    def _cache_reload_scope(self, args, _kwargs):
        # A backing is registered before __init__ submits its first task.
        # The worker reads only weak Python identity and a frozen token; it
        # never looks at the live disk-cache controller, canvas or Qt owner.
        if not args:
            return None
        with self._cache_backings_lock:
            token = self._owned_cache_backings.get(args[0])
        if token is None:
            # Synchronous scene reloads retain their existing scheduler scope.
            return self._current_scope()
        if (token.controller() is not self
                or token.generation is not self._capture_generation
                or token.epoch != self.recorder.capture_epoch):
            return None
        return token

    def _register_cache_backing(self, args, kwargs, token):
        if (threading.get_ident() != self._ui_thread or not args
                or not kwargs.get('defer_index', False)
                or token.details().get('scope') != 'gui_cache_bind'):
            return
        details = {**token.details(), "scope": "detached_cache_io", "cache_task": "restore_index"}
        captured = _MonitorToken(token.controller, token.generation, token.epoch, tuple(details.items()))
        with self._cache_backings_lock:
            self._owned_cache_backings[args[0]] = captured

    def _input_gate_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread or not args:
            return None
        gate = args[0]
        if (getattr(gate, 'canvas', None) is not self.canvas
                or not any(current is gate for current in self.canvas._native_input_gates())
                or gate.chapter is not self.canvas.chapter or gate.tiles is not self.canvas.tiles):
            return None
        return {"ownership": "monitored_editor", "scope": "gui_input_gate",
                "consumer": "native_input", "gate_id": id(gate),
                "source_id": str(gate.identifier)[:64], "queued_packets": len(gate.queue),
                "source_pending": bool(gate.pending), "released": bool(gate.released),
                "run_generation": self._run_serial}

    def _cage_details(self, jobs, lane, compute, arguments, valid):
        # Only the real private cage queue and ordinary cage functions qualify.
        # Attributes copied onto another function cannot establish ownership.
        from comic_editor.render.input_capture import cage_initial, cage_commit
        from comic_editor.ui.scene_consumers import SceneConsumers
        if (threading.get_ident() != self._ui_thread or type(jobs) is not SceneConsumers
                or jobs is not getattr(self.canvas, '_scene_consumers', None)
                or jobs.canvas is not self.canvas or jobs.stopped.is_set()
                or self.canvas.chapter is None or not callable(valid)):
            return None
        if lane == ('cage-initial',) and compute is cage_initial and len(arguments) == 1:
            phase, pending = 'cage_initial', self.canvas._cage_prepare
        elif (lane == ('cage-commit',) and compute is cage_commit and len(arguments) == 2
                and self.canvas._cage_session is not None):
            phase, pending = 'cage_commit', self.canvas._cage_commit_pending
        else:
            return None
        targets = arguments[0]
        if (pending is None or not isinstance(targets, tuple)
                or targets != tuple(self.canvas.selected_entities)):
            return None
        try:
            if not valid():
                return None
        except Exception:
            # Diagnostics never replace the ordinary validation/error path.
            return None
        return {'ownership': 'monitored_editor', 'scope': 'gui_cage_consumer',
                'consumer': phase, 'cage_request_id': id(pending),
                'cage_jobs_id': id(jobs), 'target_count': len(targets),
                'targets': tuple((str(kind)[:32], str(identifier)[:64])
                    for kind, identifier in targets[:64]), 'run_generation': self._run_serial}

    def _cage_request_scope(self, args, _kwargs):
        if (len(args) < 10 or args[5] or args[8]
                or (len(args) > 10 and args[10])):
            return None
        jobs, lane, compute, arguments = args[:4]
        details = self._cage_details(jobs, lane, compute, arguments, args[6])
        return {**details, 'scope': 'gui_cage_request',
            'document_revision': self.canvas._render_document_state().revision} if details else None

    def _cage_consumer_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread or len(args) < 2:
            return None
        jobs, job = args[:2]
        if getattr(job, 'lane', None) not in (('cage-initial',), ('cage-commit',)):
            return None
        if (job.ordered or job.detached or job.owned_source or job.key != job.lane
                or job.cancelled.is_set()):
            return None
        details = self._cage_details(jobs, job.lane, job.compute, job.arguments, job.valid)
        if details is None or job.document != self.canvas._render_document_state():
            return None
        owned = jobs.active is job or jobs.pending.get(job.key) is job
        if not owned:
            # _accept is called after active is cleared. Its frozen preparation
            # association retains identity without retaining the job or canvas.
            with self._cache_backings_lock:
                token = self._owned_cage_jobs.get(job.cancelled)
            owned = (token is not None and token.generation is self._capture_generation
                and token.epoch == self.recorder.capture_epoch
                and token.details().get('cage_job_id') == id(job)
                and token.details().get('cage_jobs_id') == id(jobs))
        return {**details, 'cage_job_id': id(job),
                'document_revision': job.document.revision} if owned else None

    def _auxiliary_consumer_scope(self, args, kwargs):
        return self._input_consumer_scope(args, kwargs) or self._cage_consumer_scope(args, kwargs)

    def _register_cage_job(self, args, _kwargs, token):
        if (threading.get_ident() != self._ui_thread or len(args) < 2
                or token.details().get('scope') != 'gui_cage_consumer'):
            return
        details = {**token.details(), 'scope': 'detached_cage_worker'}
        captured = _MonitorToken(token.controller, token.generation, token.epoch, tuple(details.items()))
        with self._cache_backings_lock:
            self._owned_cage_jobs[args[1].cancelled] = captured

    def _input_consumer_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread or len(args) < 2:
            return None
        jobs, job = args[:2]
        if getattr(jobs, 'canvas', None) is not self.canvas:
            return None
        lane = job.lane
        if not isinstance(lane, tuple) or len(lane) < 2 or lane[0] != 'native-input':
            return None
        gate = next((gate for gate in self.canvas._native_input_gates()
            if gate.jobs is jobs and id(gate) == lane[1]), None)
        details = self._input_gate_scope((gate,), {}) if gate is not None else None
        if details is None:
            return None
        requested = job.arguments[1] if len(job.arguments) > 1 else None
        return {**details, 'scope': 'gui_input_consumer',
                'document_revision': getattr(job.document, 'revision', None),
                'owned_source': bool(job.owned_source), 'detached': bool(job.detached),
                'requested_tiles': len(requested) if job.owned_source
                    and isinstance(requested, (tuple, list, set)) else None}

    def _input_capture_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread or not args:
            return None
        capture = args[0]
        if getattr(capture, 'owner', None) is not self.canvas:
            return None
        for gate in self.canvas._native_input_gates():
            job = gate.jobs.active
            if job is not None and job.capture is capture:
                details = self._input_consumer_scope((gate.jobs, job), {})
                return {**details, 'scope': 'gui_input_capture_slice'} if details is not None else None
        return None

    def _register_input_job(self, args, _kwargs, token):
        if (threading.get_ident() != self._ui_thread or len(args) < 2
                or token.details().get('scope') != 'gui_input_consumer'):
            return
        details = {**token.details(), 'scope': 'detached_input_worker'}
        captured = _MonitorToken(token.controller, token.generation, token.epoch, tuple(details.items()))
        with self._cache_backings_lock:
            self._owned_input_jobs[args[1].cancelled] = captured

    def _input_worker_scope(self, args, kwargs):
        cancelled = args[3] if len(args) > 3 else kwargs.get('cancelled')
        if cancelled is None:
            return None
        with self._cache_backings_lock:
            token = self._owned_input_jobs.get(cancelled)
            cage = self._owned_cage_jobs.get(cancelled) if token is None else None
        if cage is not None:
            from comic_editor.render.input_capture import cage_initial, cage_commit
            compute = args[0] if args else kwargs.get('compute')
            expected = {'cage_initial': cage_initial, 'cage_commit': cage_commit}.get(
                cage.details().get('consumer'))
            token = cage if compute is expected else None
        if (token is None or token.controller() is not self
                or token.generation is not self._capture_generation
                or token.epoch != self.recorder.capture_epoch):
            return None
        return token

    def _input_prepare_scope(self, _args, _kwargs):
        # These module functions also support unrelated auxiliary consumers.
        # Positive frozen native-input ownership is required; no Qt inspection
        # or a generic scheduler token is sufficient on the worker.
        token = self._current_scope()
        return token if (threading.get_ident() != self._ui_thread and token is not None
            and token.details().get('scope') == 'detached_input_worker') else None

    def _register_monitor_object(self, owner, scope):
        # Registration happens before the first GUI sample or log submission.
        # A direct recorder clear cannot adopt an older object's next operation
        # into the replacement epoch, even if the controller stays enabled.
        if threading.get_ident() != self._ui_thread:
            return
        details = {'ownership': 'monitored_editor', 'scope': scope,
                   'run_generation': self._run_serial}
        if scope == 'detached_monitor_log':
            details['writer_id'] = id(owner)
        token = _MonitorToken(weakref.ref(self), self._capture_generation,
            self.recorder.capture_epoch, tuple(details.items()))
        with self._cache_backings_lock:
            self._owned_monitor_objects[owner] = token

    def _monitor_object_scope(self, owner):
        with self._cache_backings_lock:
            token = self._owned_monitor_objects.get(owner)
        return token if (token is not None and token.controller() is self
            and token.generation is self._capture_generation
            and token.epoch == self.recorder.capture_epoch) else None

    def _resource_sample_scope(self, args, _kwargs):
        if (threading.get_ident() != self._ui_thread or not args
                or args[0] is not self._resources):
            return None
        token = self._monitor_object_scope(args[0])
        return token if token is not None and token.details().get('scope') == 'gui_monitor_resource' else None

    def _monitor_metrics_scope(self, args, _kwargs):
        if (threading.get_ident() != self._ui_thread or not args or args[0] is not self
                or self._resources is None):
            return None
        token = self._resource_sample_scope((self._resources,), {})
        return _MonitorToken(token.controller, token.generation, token.epoch,
            tuple({**token.details(), 'scope': 'gui_monitor_metrics'}.items())) if token is not None else None

    def _monitor_display_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread or not args:
            return None
        dialog = args[0]
        if (dialog is not getattr(self.window, '_performance_monitor_dialog', None)
                or dialog.controller is not self or not dialog.isVisible()):
            return None
        token = self._monitor_object_scope(dialog)
        if token is None or token.details().get('scope') != 'gui_monitor_display':
            return None
        details = {**token.details(), 'dialog_id': id(dialog),
            'dialog_minimized': bool(dialog.isMinimized()), 'tab_index': dialog.tabs.currentIndex()}
        return _MonitorToken(token.controller, token.generation, token.epoch, tuple(details.items()))

    def _monitor_display_snapshot_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread or not args or args[0] is not self:
            return None
        token = self._current_scope()
        dialog = getattr(self.window, '_performance_monitor_dialog', None)
        if (token is None or token.details().get('scope') != 'gui_monitor_display'
                or dialog is None or token.details().get('dialog_id') != id(dialog)):
            return None
        return _MonitorToken(token.controller, token.generation, token.epoch,
            tuple({**token.details(), 'scope': 'gui_monitor_display_snapshot'}.items()))

    def _log_writer_scope(self, args, _kwargs):
        # The exact auto-log writer is a Python object; workers never inspect
        # widgets or query the current document to establish this association.
        if (threading.get_ident() == self._ui_thread or not args
                or args[0] is not self._writer):
            return None
        token = self._monitor_object_scope(args[0])
        return token if token is not None and token.details().get('scope') == 'detached_monitor_log' else None

    def _current_log_scope(self):
        token = self._current_scope()
        # Reading the Python writer pointer is safe; it carries no Qt API call.
        # Keep this local alive throughout a child probe. GUI stop/start may
        # replace self._writer immediately after its identity is validated.
        writer = self._writer
        return (token, writer) if (token is not None and writer is not None
            and token.details().get('scope') == 'detached_monitor_log'
            and token.details().get('writer_id') == id(writer)) else None

    def _log_snapshot_scope(self, args, _kwargs):
        current = self._current_log_scope() if args and args[0] is self else None
        return current[0] if current is not None else None

    def _log_serialize_scope(self, args, kwargs):
        current = self._current_log_scope()
        if current is None:
            return None
        token, writer = current
        value = args[0] if args else kwargs.get('obj')
        if (type(value) is not dict or value.get('schema_version') != 1
                or not {'metadata', 'timeline', 'log'} <= value.keys()
                or type(value['log']) is not dict
                or value['log'].get('started_utc') != writer._started_utc):
            return None
        return token

    def _log_write_scope(self, args, _kwargs):
        current = self._current_log_scope()
        if current is None or not args:
            return None
        token, writer = current
        role = ('capture_json' if args[0] == writer.json_path else
                'summary' if args[0] == writer.summary_path else None)
        return _MonitorToken(token.controller, token.generation, token.epoch,
            tuple({**token.details(), 'file_role': role}.items())) if role is not None else None

    def _canvas_painter_scope(self, args, _kwargs):
        # Native painter boundaries may block after the presenter returns.
        # Observe only the current GUI owner's active canvas painter, never
        # an export/worker painter or a painter in another editor window.
        if threading.get_ident() != self._ui_thread:
            return None
        token = self._current_scope()
        if token is None or token.details().get('ownership') != 'monitored_editor':
            return None
        painter = args[0] if args else None
        if painter is None or not painter.isActive() or painter.device() is not self.canvas:
            return None
        return token.details()

    def _pending_painter_scope(self, args, kwargs):
        if not args or args[0] is not self.canvas:
            return None
        return self._canvas_painter_scope(args[1:], kwargs)

    def _border_painter_scope(self, args, kwargs):
        details = self._canvas_painter_scope(args, kwargs)
        return {**details, 'presentation_owner': 'document_border'} if details is not None else None

    def _border_construction_scope(self, _args, _kwargs):
        # Construction has no painter argument. Inherit only a positively
        # guarded GUI border caller, not a generic export/worker GUI scope.
        if threading.get_ident() != self._ui_thread:
            return None
        token = self._current_scope()
        if token is None:
            return None
        details = token.details()
        return details if (details.get('ownership') == 'monitored_editor'
            and details.get('presentation_owner') == 'document_border') else None

    def _projection_trim_scope(self, args, _kwargs):
        if threading.get_ident() != self._ui_thread:
            return None
        token = self._current_scope()
        if (token is None or token.details().get('ownership') != 'monitored_editor'
                or not args or args[0] is not self.canvas._document_projection):
            return None
        return token.details()

    @staticmethod
    def _render_details(args, kwargs, name=""):
        # No QWidget reads, image bits, semantic key repr or serialization.
        details = {}
        for value in args[:4]:
            identifier = getattr(value, "object_id", None) or getattr(value, "layer_id", None)
            if identifier:
                details["entity"] = {"id": str(identifier)[:64], "type": type(value).__name__}
                break
        if name == "render_distort":
            modifier = args[2] if len(args) > 2 else kwargs.get("modifier")
            if modifier is not None:
                details["modifier"] = {"id": str(modifier.modifier_id)[:64],
                                       "type": str(modifier.modifier_type)[:64]}
            details["pixel_scale"] = args[6] if len(args) > 6 else kwargs.get("pixel_scale", 1.)
        return details

    def _already_patched(self, owner, name):
        return any(item[0] is owner and item[1] == name for item in self._patches)

    def _mark_contact_frame(self, boundary, epoch):
        if self._critical_paint_epoch != epoch:
            self._critical_paint_boundaries.clear()
            self._critical_paint_epoch = epoch
        self._critical_paint_boundaries.add(boundary)

    def _receipt_current(self, receipt, epoch):
        return (receipt is not None and not receipt['finished']
            and receipt['epoch'] == epoch and receipt['generation'] is self._capture_generation
            and receipt['chapter'] == id(self.canvas.chapter)
            and receipt['tiles'] == id(self.canvas.tiles))

    def _cancel_receipt(self, receipt, reason):
        if receipt is None or receipt['finished']:
            return
        receipt['finished'] = True
        self.recorder.record_event('canvas.contact_frame.cancelled', category='diagnostic',
            details={'receipt_serial': receipt['serial'], 'source_id': receipt['source_id'],
                'reason': reason, 'tool': receipt['tool']},
            capture_epoch=receipt['epoch'], critical=True)

    def _clear_input_receipts(self, reason):
        self._cancel_receipt(self._input_receipt, reason)
        for receipt in self._gate_input_receipts.values():
            self._cancel_receipt(receipt, reason)
        for _journal, receipt in self._deferred_input_receipts.values():
            self._cancel_receipt(receipt, reason)
        self._input_receipt = self._current_input_receipt = None
        self._gate_input_receipts.clear()
        self._deferred_input_receipts.clear()

    def _receipt_begin(self, name, epoch):
        journal_name = ('_paint_brush_deferred' if 'paint_brush' in name else '_raster_deferred_strokes')
        journal = getattr(self.canvas, journal_name, ()) or ()
        if name.startswith('_start_deferred_'):
            entry = self._deferred_input_receipts.pop(id(journal[0]), None) if journal else None
            receipt = entry[1] if entry is not None and entry[0] is journal[0] else None
        else:
            receipt = self._current_input_receipt or self._input_receipt
            if receipt is self._input_receipt:
                self._input_receipt = None
        return (receipt if self._receipt_current(receipt, epoch) else None,
                journal_name, len(journal))

    def _bind_input_receipt(self, receipt, journal_name, before):
        if not self._receipt_current(receipt, self.recorder.capture_epoch):
            return
        journal = getattr(self.canvas, journal_name, ()) or ()
        if len(journal) > before:
            entry = journal[-1]
            if len(self._deferred_input_receipts) >= 512:
                _old_journal, old_receipt = self._deferred_input_receipts.pop(
                    next(iter(self._deferred_input_receipts)))
                self._cancel_receipt(old_receipt, 'diagnostic_capacity')
            self._deferred_input_receipts[id(entry)] = entry, receipt
            return
        gate_name = '_paint_brush_tile_input' if journal_name == '_paint_brush_deferred' else '_raster_tile_input'
        gate = getattr(self.canvas, gate_name, None)
        if (gate is not None and not gate.closed and gate.identifier == receipt['source_id']
                and gate.chapter is self.canvas.chapter and gate.tiles is self.canvas.tiles):
            self._gate_input_receipts[gate] = receipt
        else:
            self._cancel_receipt(receipt, 'not_admitted')

    def _arm_feedback_contact(self, epoch):
        # Only an actual native begin can arm this diagnostic. A Ctrl-click's
        # input receipt in a drawing tool does not establish a new contact.
        canvas = self.canvas
        from comic_editor.ui.tile_input import TileInputGate
        gate = getattr(canvas, '_paint_brush_tile_input' if _value(canvas.tool) == 'brush'
                       else '_raster_tile_input', None)
        if (not isinstance(gate, TileInputGate) or gate.closed or gate.error is not None
                or gate.canvas is not canvas or gate.chapter is not canvas.chapter
                or gate.tiles is not canvas.tiles or gate.identifier != canvas.selected_id
                or canvas.selected_kind != 'object'):
            return
        current = self._feedback_contact_wait
        if (current is not None and current['gate']() is gate and current['epoch'] == epoch
                and current['generation'] is self._capture_generation):
            return
        self._feedback_contact_serial += 1
        self._feedback_contact_wait = {'gate': weakref.ref(gate), 'epoch': epoch,
            'generation': self._capture_generation, 'serial': self._feedback_contact_serial,
            'source_id': gate.identifier, 'tool': _value(canvas.tool),
            'receipt': self._current_input_receipt or self._gate_input_receipts.get(gate)}

    def _observe_feedback_batch(self, owner, args, result):
        paint = self._active_feedback_paint
        if (paint is None or owner is not self.canvas._scene_controller or not args
                or type(result) is not tuple or len(result) != 2 or not isinstance(result[1], int)):
            return
        state = owner._filtered_state
        paint['batch'] = {'requested_revision': getattr(args[0], 'revision', -1),
            'filtered_count': result[1], 'filtered_state_id': id(state) if state is not None else None,
            'filtered_revision': getattr(state, 'revision', None),
            'filtered_completed_revision': getattr(state, 'completed_revision', None),
            'filtered_pending_generations': bool(getattr(state, 'generations', None))}

    def _finish_feedback_paint(self, paint, duration, cpu_duration, details):
        waiting = self._feedback_contact_wait
        if (waiting is None or waiting['epoch'] != self.recorder.capture_epoch
                or waiting['generation'] is not self._capture_generation
                or paint['epoch'] != waiting['epoch']):
            self._feedback_contact_wait = None
            return
        canvas = self.canvas
        gate = waiting['gate']()
        gate_name = '_paint_brush_tile_input' if waiting['tool'] == 'brush' else '_raster_tile_input'
        if (gate is None or gate is not getattr(canvas, gate_name, None)
                or gate.closed or gate.error is not None or gate.canvas is not canvas
                or gate.chapter is not canvas.chapter or gate.tiles is not canvas.tiles
                or canvas.selected_kind != 'object' or canvas.selected_id != waiting['source_id']
                or _value(canvas.tool) != waiting['tool']):
            self._cancel_receipt(waiting.get('receipt'), 'owner_changed')
            self._feedback_contact_wait = None
            return
        presentation = getattr(canvas, '_document_presentation_stats', None)
        backend = str(getattr(presentation, 'backend', ''))[:32]
        batch = paint.get('batch')
        # This batch hook is reached after the ordinary projection clears its
        # old coverage flag. Same-paint metadata rejects a prior true flag, an
        # older count, and fast paths which bypass that reset.
        if (batch is None or gate.busy or backend == 'gpu-pending'
                or getattr(canvas, '_gpu_presentation_pending', False)
                or not getattr(canvas, '_raster_feedback_contact_covered', False)):
            return
        state = canvas._scene_controller._filtered_state
        if state is not None and (batch['filtered_state_id'] != id(state)
                or batch['filtered_count'] <= 0 or batch['filtered_pending_generations']
                or batch['filtered_revision'] != batch['requested_revision']
                or batch['filtered_completed_revision'] != batch['requested_revision']):
            return
        if state is None and batch['filtered_state_id'] is not None:
            return
        outcome = {**details, **batch, 'contact_serial': waiting['serial'],
            'source_id': waiting['source_id'], 'gate_id': id(gate),
            'presentation_backend': backend,
            'frame_pending': bool(getattr(canvas, '_projection_frame_pending', False)),
            'feedback_contact_covered': True,
            'presented_revision': getattr(canvas, '_projection_presented_revision', -1),
            'evidence': 'current feedback coverage metadata; no pixel readback'}
        self._feedback_contact_wait = None
        self.recorder.record_event('canvas.contact_frame.first_covered', duration,
            category='frame', details=outcome, capture_epoch=waiting['epoch'], critical=True)
        if cpu_duration is not None:
            self.recorder.record_event('canvas.contact_frame.first_covered.thread_cpu', cpu_duration,
                category='gui_cpu', details={**outcome, 'clock': 'thread_cpu'},
                capture_epoch=waiting['epoch'], critical=True)
        receipt = waiting.get('receipt')
        if self._receipt_current(receipt, waiting['epoch']):
            receipt['finished'] = True
            self.recorder.record_event('canvas.contact_frame.receipt_to_first_covered',
                max(0., (time.perf_counter() - receipt['received']) * 1000.), category='latency',
                details={**outcome, 'receipt_serial': receipt['serial'],
                    'input_kind': receipt['input_kind'],
                    'timing_boundary': 'application event receipt to completed covered paint; excludes driver/scanout'},
                capture_epoch=waiting['epoch'], critical=True)

    def _patch_owned(self, owner, name, label, *, start_scope=None, cache=False, cpu=False,
                     require_scope=False, timeline_min_ms=None, register_cache=False, critical=False,
                     register_input=False, mask_unowned_scope=False, register_cage=False,
                     scope_labels=None, cpu_scopes=(), begin_scopes=(), critical_min_ms=None,
                     text_draw=False):
        if self._already_patched(owner, name):
            return
        original = getattr(owner, name, None)
        if not callable(original):
            return
        own = getattr(owner, "__dict__", {}).get(name, _MISSING)
        reference, generation = weakref.ref(self), self._capture_generation
        # A bound scope factory would retain the controller in a class wrapper.
        scope_method = start_scope.__name__ if start_scope is not None else None

        @wraps(original)
        def measured(*args, **kwargs):
            controller = reference()
            if controller is None:
                return original(*args, **kwargs)
            # Read epoch before generation validation. A restart between these
            # reads is rejected; after validation the old epoch pins recording.
            epoch = controller.recorder.capture_epoch
            if (not controller.enabled
                    or generation is not controller._capture_generation):
                return original(*args, **kwargs)
            current = controller._current_scope()
            # Old in-flight TLS must never be adopted by newly installed probes.
            if getattr(_monitor_scope, "value", None) is not None and current is None:
                return original(*args, **kwargs)
            metadata = getattr(controller, scope_method)(args, kwargs) if scope_method else None
            if scope_method and metadata is None:
                if mask_unowned_scope:
                    # Nested foreign diagnostic calls must not lend their
                    # children the outer owner's frozen token.
                    with controller._owned_scope({'ownership': 'unattributed',
                            'scope': 'foreign_owned_call'}):
                        return original(*args, **kwargs)
                return original(*args, **kwargs)
            captured = metadata if isinstance(metadata, _MonitorToken) else None
            if captured is not None:
                if (captured.controller() is not controller or captured.generation is not generation
                        or captured.epoch != epoch):
                    return original(*args, **kwargs)
                metadata = captured.details()
            if require_scope:
                if (current is None or current.details().get("scope") != "detached_worker"
                        or any(current.details().get(key) != metadata.get(key) for key in
                               ("scheduler_role", "consumer", "demand_serial", "document_revision"))):
                    return original(*args, **kwargs)
                token = current
            else:
                token = (captured or _MonitorToken(reference, generation, epoch, tuple(metadata.items()))
                         if metadata is not None else current)
            if token is None:
                return original(*args, **kwargs)
            if token.details().get("ownership") != "monitored_editor":
                return original(*args, **kwargs)
            if register_cache:
                controller._register_cache_backing(args, kwargs, token)
            if register_input:
                controller._register_input_job(args, kwargs, token)
            if register_cage:
                controller._register_cage_job(args, kwargs, token)
            scope = token.details().get('scope')
            selected_label = scope_labels.get(scope, label) if scope_labels else label
            details = {**token.details(), **controller._render_details(args, kwargs),
                       "capture_epoch": token.epoch, "thread_id": threading.get_ident(),
                       "thread": "gui" if threading.get_ident() == controller._ui_thread else "worker"}
            cpu_clock = getattr(time, "thread_time", None) if cpu or scope in cpu_scopes else None
            started_cpu = cpu_clock() if cpu_clock is not None else None
            critical_duration = critical_cpu = critical_end = None
            critical_origin = controller.recorder._origin if critical_min_ms is not None else None
            raised = None
            result = _MISSING
            retain_timeline = True
            text_serial = controller._text_draw_serial(token.epoch) if text_draw else None
            if scope in begin_scopes:
                controller.recorder.record_event(selected_label + '.begin', details=details,
                    capture_epoch=token.epoch, critical=True)
            try:
                if timeline_min_ms is None:
                    with controller._owned_scope(token), controller.recorder.measure(
                            selected_label, details=details, capture_epoch=token.epoch, critical=critical):
                        if critical_min_ms is None:
                            result = original(*args, **kwargs)
                        else:
                            started_critical = time.perf_counter()
                            cpu_critical = cpu_clock() if cpu_clock is not None else None
                            try:
                                result = original(*args, **kwargs)
                            finally:
                                completed_critical = time.perf_counter()
                                critical_duration = (completed_critical - started_critical) * 1000.
                                critical_end = (completed_critical - critical_origin) * 1000.
                                if cpu_critical is not None:
                                    critical_cpu = (cpu_clock() - cpu_critical) * 1000.
                else:
                    # Only newly added high-frequency leaf probes use this
                    # path. Keep every wall/CPU summary sample but retain slow
                    # or failed calls as rows, so input events survive longer.
                    # Parent worker envelopes still expose active operations.
                    started_wall = time.perf_counter()
                    try:
                        with controller._owned_scope(token):
                            result = original(*args, **kwargs)
                    except BaseException as error:
                        raised = type(error).__name__
                        raise
                    finally:
                        duration = (time.perf_counter() - started_wall) * 1000.
                        if critical_min_ms is not None:
                            critical_duration = duration
                            critical_end = (time.perf_counter() - critical_origin) * 1000.
                        retain_timeline = raised is not None or duration >= timeline_min_ms
                        final = {**details, 'exception_type': raised} if raised else details
                        controller.recorder.record_event(selected_label, duration, category='phase',
                            details=final, capture_epoch=token.epoch,
                            retain_timeline=retain_timeline, critical=critical)
            except BaseException as error:
                raised = type(error).__name__
                raise
            finally:
                duration_cpu = None
                if started_cpu is not None:
                    final = {**details, "clock": "thread_cpu"}
                    if raised is not None:
                        final["exception_type"] = raised
                    duration_cpu = (cpu_clock() - started_cpu) * 1000.
                    controller.recorder.record_event(selected_label + ".thread_cpu",
                        duration_cpu,
                        category="gui_cpu" if details['thread'] == 'gui' else "worker_cpu",
                        details=final, capture_epoch=token.epoch,
                        retain_timeline=retain_timeline, critical=critical)
                if critical_duration is not None:
                    if critical_duration >= critical_min_ms:
                        # Keep the ordinary span and its summary intact. A
                        # sparse auxiliary outcome preserves this inclusive
                        # endpoint after leaf traffic evicts the main row.
                        final = {**details, 'source_phase': selected_label,
                                 'source_start_ms': critical_end - critical_duration,
                                 'source_end_ms': critical_end,
                                 'evidence': 'slow owned capture outcome; inclusive auxiliary span'}
                        if raised is not None:
                            final['exception_type'] = raised
                        controller.recorder.record_event(selected_label + '.slow', critical_duration,
                            category='diagnostic', details=final, capture_epoch=token.epoch,
                            retain_timeline=False, critical=True)
                        if critical_cpu is not None:
                            controller.recorder.record_event(selected_label + '.slow.thread_cpu', critical_cpu,
                                category='gui_cpu' if details['thread'] == 'gui' else 'worker_cpu',
                                details={**final, 'clock': 'thread_cpu'}, capture_epoch=token.epoch,
                                retain_timeline=False, critical=True)
                if selected_label in {'scene.capture_slice', 'cage.capture_slice'}:
                    capture = args[0] if args else None
                    step_ms = getattr(capture, 'max_step_ms', 0.)
                    if step_ms >= 4.:
                        controller.recorder.record_event('scene.capture_step.slow', step_ms,
                            category='diagnostic', details={**details,
                                'capture_stage': str(getattr(capture, 'max_step_stage', ''))[:96],
                                'step_ms': round(step_ms, 3),
                                'slice_complete': result if isinstance(result, bool) else None,
                                'exception_type': raised}, capture_epoch=token.epoch,
                            retain_timeline=False, critical=True)
            if cache:
                # Worker cache hit counters use the recorder's existing lock.
                controller.recorder.record_event(selected_label + (".miss" if result is None else ".hit"),
                    category="cache", details=details, capture_epoch=token.epoch)
            if text_serial is not None:
                controller._observe_text_draw(args, kwargs, result, token.epoch, text_serial)
            return result

        # Class static methods must keep their descriptor or instance callers
        # gain an extra positional argument before reaching the original API.
        setattr(owner, name, staticmethod(measured) if isinstance(own, staticmethod) else measured)
        self._patches.append((owner, name, own, measured))

    def _text_owner(self, identifier):
        """Look up one model owner without reading its content or geometry."""
        from comic_editor.core.models import TextObject
        chapter = getattr(self.canvas, 'chapter', None)
        owner = chapter.objects.get(identifier) if chapter is not None else None
        return owner if isinstance(owner, TextObject) else None

    @staticmethod
    def _text_render_flags(source):
        # These are bounded SCENE_FIELDS values/presence flags only. Never
        # detach a source map, a geometry value, a QTextDocument or a QImage.
        read = source.get if isinstance(source, dict) else lambda name, default=None: getattr(source, name, default)
        return {
            'text_editing': bool(read('_text_editing', False)),
            'exclude_text': bool(read('_render_exclude_text', False)),
            'excluded_object_id': str(read('_render_excluded_object_id', '') or '')[:160],
            'underlay_object_id': str(read('_live_underlay_object_id', '') or '')[:160],
            'transform_preview': read('_transform_preview_quad') is not None,
            'cage_preview': read('_cage_session') is not None,
            'selection_before': read('_selection_before_tiles') is not None,
            'selection_transform': read('_selection_transform_quad') is not None,
        }

    def _text_context_basis(self):
        canvas = self.canvas
        return (id(canvas.chapter), id(canvas.tiles), id(canvas.images),
                canvas.selected_kind, canvas.selected_id, _value(canvas.tool),
                canvas.command_stack.revision, canvas._document_projection.revision)

    def _arm_text_presentation(self, identifier, action, epoch):
        if (not self.enabled or threading.get_ident() != self._ui_thread
                or epoch != self.recorder.capture_epoch):
            return
        owner = self._text_owner(identifier)
        if owner is None:
            return
        self._text_presentation_serial += 1
        self._text_presentation_wait = {
            'epoch': epoch, 'generation': self._capture_generation,
            'serial': self._text_presentation_serial, 'target': str(identifier)[:160],
            'target_owner': id(owner), 'action': action,
            'basis': self._text_context_basis(), 'first_paint': False,
            'scene_owner': id(self.canvas._scene_controller),
            'configuration': _MISSING,
            'deadline': time.perf_counter() + 60.,
        }
        self.recorder.record_event('canvas.text_presentation.action', category='diagnostic',
            details={'ownership': 'monitored_editor', 'scope': 'gui_text_presentation',
                     'run_generation': self._run_serial, 'text_action': action,
                     'text_action_serial': self._text_presentation_serial,
                     'text_target': str(identifier)[:160], 'text_target_owner': id(owner),
                     'requested_revision': self.canvas._document_projection.revision,
                     'live_flags': self._text_render_flags(self.canvas)},
            capture_epoch=epoch, critical=True)

    def _observe_text_action(self, name, args, kwargs, result, epoch, font_before):
        if result is False:
            return
        canvas = self.canvas
        if name == 'set_selection' and canvas.selected_kind == 'object':
            requested = args[1] if len(args) > 1 else kwargs.get('entity_id')
            if requested == canvas.selected_id and self._text_owner(requested) is not None:
                self._arm_text_presentation(requested, 'selection', epoch)
        elif name == '_change_selected_text_property' and font_before is not None:
            identifier, revision = font_before
            if canvas.command_stack.revision != revision and canvas.selected_id == identifier:
                self._arm_text_presentation(identifier, 'floating_font', epoch)
        elif name == '_publish_change_set' and kwargs.get('action') in {'undo', 'redo'}:
            change = args[0] if args else kwargs.get('change')
            # A history action can run after selecting a different object.
            # Only explicit changed Text refs arm this diagnostic; there is no
            # whole-chapter traversal or conservative guess about undo intent.
            for item in getattr(change, 'entities', ())[:32]:
                kind, identifier = item.entity
                if kind == 'object' and self._text_owner(identifier) is not None:
                    self._arm_text_presentation(identifier, kwargs['action'], epoch)
                    break

    def _text_draw_serial(self, epoch):
        """Only an armed Text frame can observe actual submitted groups."""
        if threading.get_ident() != self._ui_thread:
            return None
        wait, paint = self._text_presentation_wait, self._active_feedback_paint
        if (wait is None or paint is None or paint['epoch'] != epoch
                or wait['epoch'] != epoch or epoch != self.recorder.capture_epoch
                or wait['generation'] is not self._capture_generation or not self.enabled
                or time.perf_counter() > wait['deadline']
                or self._text_context_basis() != wait['basis']
                or id(self.canvas._scene_controller) != wait['scene_owner']
                or id(self._text_owner(wait['target'])) != wait['target_owner']):
            return None
        if wait['first_paint'] and (getattr(self.canvas, '_projection_frame_pending', False)
                or getattr(self.canvas, '_projection_provisional_visible', False)):
            return None
        return wait['serial']

    def _observe_text_draw(self, args, kwargs, result, epoch, serial):
        # All reference matching happens on the GUI during the actual draw
        # invocation. Only detached scalar metadata survives until paint ends.
        if self._text_draw_serial(epoch) != serial:
            return
        paint = self._active_feedback_paint
        draws = paint.setdefault('text_draws', [])
        call = paint.get('text_draw_call_count', 0) + 1
        paint['text_draw_call_count'] = call
        if call > 4:
            paint['text_draws_truncated'] = True
            return
        groups = args[1] if len(args) > 1 else kwargs.get('groups')
        if type(groups) not in (tuple, list):
            return
        canvas, scene = self.canvas, self.canvas._scene_controller
        desired = scene.desired[0] if scene.desired is not None else None
        candidates = []

        def document_details(document):
            return {
                'revision': getattr(document, 'revision', None),
                'live_preview': bool(getattr(document, 'live_preview', False)),
                'source_identity': list(getattr(document, 'identity', ())[:3]),
                'configuration_matches_desired': bool(desired is not None
                    and getattr(document, 'configuration', None) == desired.configuration),
            }

        def add_batch(origin, batch, metadata):
            if type(batch) not in (tuple, list) or len(batch) > 8:
                return
            for phase, tiles in batch:
                if type(tiles) in (tuple, list) and len(tiles) <= 128:
                    candidates.append((tiles, {'origin': origin, 'phase': phase,
                                               'origin_flags_available': False, **metadata}))

        previous = getattr(canvas, '_projection_completed_view', None)
        if previous is not None:
            add_batch('completed_view', previous[1], {
                'revision': previous[2],
                'source_identity': list(previous[0][:3]),
                'configuration_matches_desired': bool(desired is not None
                    and previous[0] == desired.configuration),
                'origin_flags_unavailable_reason': 'completed view retains configuration and revision only',
            })
        for name in ('preview', 'overview'):
            value = getattr(scene, name, None)
            if value is not None:
                document, tile = value
                metadata = document_details(document)
                snapshot = scene._preview_snapshot if name == 'preview' else None
                if snapshot is not None and getattr(snapshot, 'document', None) == document:
                    state = getattr(snapshot, 'state', None)
                    if type(state) is dict:
                        metadata.update(origin_flags_available=True,
                            origin_flags=self._text_render_flags(state), snapshot_owner=id(snapshot))
                add_batch(name, ((None, (tile,)),), metadata)
        continuity = getattr(scene, 'raster_continuity', None)
        if continuity is not None and continuity.document is not None:
            add_batch('raster_continuity', continuity.batch,
                      document_details(continuity.document))
        remaining = 128
        if len(groups) > 8:
            paint['text_draws_truncated'] = True
        for group_index, group in enumerate(groups[:8]):
            tiles = getattr(group, 'tiles', ())
            count = len(tiles) if type(tiles) in (tuple, list) else None
            item = {'draw_call': call, 'group_index': group_index,
                'group_count': len(groups), 'tile_count': count,
                'result_backend': str(getattr(result, 'backend', ''))[:32],
                'gpu_pending_replay': getattr(result, 'backend', '') == 'gpu-pending'}
            origins = []
            if count is not None and count <= remaining:
                remaining -= count
                origins = [metadata for source, metadata in candidates if count
                    if len(source) == count and all(left is right for left, right in zip(source, tiles))]
            else:
                item['reference_match_truncated'] = True
                paint['text_draws_truncated'] = True
            # The group tag is observable, but an old batch may carry a current
            # document tag. It is not evidence of the pixels' original basis.
            context = getattr(group, 'native_context', None)
            from comic_editor.render.service import RenderDocument
            if type(context) is tuple and context and isinstance(context[0], RenderDocument):
                item.update(('document_tag_' + key, value)
                            for key, value in document_details(context[0]).items())
            for origin in origins or [{'origin': 'unmatched', 'origin_flags_available': False}]:
                if len(draws) >= 24:
                    paint['text_draws_truncated'] = True
                    return
                draws.append({**item, **origin})

    def _finish_text_paint(self, epoch, duration, cpu_duration, details):
        wait = self._text_presentation_wait
        if wait is None:
            return
        if (wait['epoch'] != epoch or epoch != self.recorder.capture_epoch
                or wait['generation'] is not self._capture_generation
                or not self.enabled or threading.get_ident() != self._ui_thread
                or time.perf_counter() > wait['deadline']
                or self._text_context_basis() != wait['basis']
                or id(self.canvas._scene_controller) != wait['scene_owner']
                or id(self._text_owner(wait['target'])) != wait['target_owner']):
            self._text_presentation_wait = None
            return
        canvas = self.canvas
        scene = canvas._scene_controller
        desired = scene.desired[0] if scene.desired is not None else None
        snapshot = scene.snapshot
        document = getattr(snapshot, 'document', None)
        state = getattr(snapshot, 'state', None)
        identity = wait['basis'][:3]
        desired_configuration = getattr(desired, 'configuration', None)
        snapshot_configuration = getattr(document, 'configuration', None)
        completed = getattr(canvas, '_projection_completed_view', None)
        stats = getattr(canvas, '_document_presentation_stats', None)
        revision = canvas._document_projection.revision
        if (desired is not None and getattr(desired, 'identity', None) == identity
                and getattr(desired, 'revision', -1) == revision):
            if wait['configuration'] is _MISSING:
                wait['configuration'] = desired_configuration
            elif wait['configuration'] != desired_configuration:
                self._text_presentation_wait = None
                return
        pending = bool(getattr(canvas, '_projection_frame_pending', False))
        gpu_pending = bool(getattr(canvas, '_gpu_presentation_pending', False))
        provisional = bool(getattr(canvas, '_projection_provisional_visible', False))
        backend = str(getattr(stats, 'backend', ''))[:32]
        snapshot_current = (document is not None and desired is not None
            and getattr(document, 'identity', None) == identity
            and getattr(desired, 'identity', None) == identity
            and getattr(document, 'revision', -1) == revision
            and getattr(desired, 'revision', -1) == revision
            and snapshot_configuration == desired_configuration)
        paint = self._active_feedback_paint or {}
        # Keep the Text evidence before optional generic phase metadata. The
        # recorder's existing 32-key/depth-four bounds remain unchanged.
        provenance = [{'observation': 'submitted_group_references',
            'call_count': paint.get('text_draw_call_count', 0),
            'truncated': bool(paint.get('text_draws_truncated', False))},
            *paint.get('text_draws', [])]
        final = {'ownership': 'monitored_editor', 'scope': 'gui_text_presentation',
            'draw_group_provenance': provenance,
            'text_action': wait['action'], 'text_action_serial': wait['serial'],
            'text_target': wait['target'], 'text_target_owner': wait['target_owner'],
            'requested_revision': revision,
            'presented_revision': getattr(canvas, '_projection_presented_revision', -1),
            'frame_pending': pending, 'gpu_pending': gpu_pending, 'provisional_visible': provisional,
            'presentation_backend': backend, 'live_flags': self._text_render_flags(canvas),
            'snapshot_flags': self._text_render_flags(state) if isinstance(state, dict) else None,
            'snapshot_owner': id(snapshot) if snapshot is not None else None,
            'scene_owner': id(scene), 'source_owner_identity': list(identity),
            'snapshot_source_identity': list(getattr(document, 'identity', ())) if document is not None else None,
            'snapshot_revision': getattr(document, 'revision', None),
            'snapshot_selected_id': str(state.get('selected_id', '') or '')[:160] if isinstance(state, dict) else None,
            'snapshot_live_preview': bool(getattr(document, 'live_preview', False)),
            'desired_revision': getattr(desired, 'revision', None),
            'desired_config_identity': id(desired_configuration) if desired_configuration is not None else None,
            'snapshot_config_identity': id(snapshot_configuration) if snapshot_configuration is not None else None,
            'snapshot_config_matches': snapshot_configuration == desired_configuration if desired is not None and document is not None else False,
            'snapshot_basis_current': snapshot_current,
            'completed_config_matches': bool(completed is not None and desired is not None
                                             and completed[0] == desired_configuration),
            'feedback_target': str(getattr(scene, 'feedback_target', '') or '')[:160],
            'filtered_owner': id(scene._filtered_state) if scene._filtered_state is not None else None,
            'scene_error': bool(scene.error),
            'evidence': 'paint metadata only; no pixel or glyph visibility observation'}
        final.update((key, value) for key, value in details.items() if key not in final)
        names = []
        if not wait['first_paint']:
            wait['first_paint'] = True
            names.append('first_paint')
        if (snapshot_current and not pending and not gpu_pending and not provisional and backend not in {'', 'gpu-pending'}
                and final['presented_revision'] == revision and not scene.error
                and (completed is None or final['completed_config_matches'])):
            names.append('first_settled')
            self._text_presentation_wait = None
        for suffix in names:
            name = 'canvas.text_presentation.' + suffix
            self.recorder.record_event(name, duration, category='diagnostic', details=final,
                capture_epoch=epoch, critical=True)
            if cpu_duration is not None:
                self.recorder.record_event(name + '.thread_cpu', cpu_duration,
                    category='gui_cpu', details={**final, 'clock': 'thread_cpu'},
                    capture_epoch=epoch, critical=True)

    def _patch(self, owner, name, label, category="phase", *, transition=False, cache=False,
               owned_label=None, cpu=False, critical=False):
        if self._already_patched(owner, name):
            return
        original = getattr(owner, name, None)
        if not callable(original):
            return
        own = getattr(owner, "__dict__", {}).get(name, _MISSING)
        reference, generation = weakref.ref(self), self._capture_generation
        global_probe = isinstance(owner, (type, ModuleType))

        @wraps(original)
        def measured(*args, **kwargs):
            controller = reference()
            if controller is None:
                return original(*args, **kwargs)
            epoch = controller.recorder.capture_epoch
            if (not controller.enabled
                    or generation is not controller._capture_generation):
                return original(*args, **kwargs)
            current = controller._current_scope()
            if getattr(_monitor_scope, "value", None) is not None and current is None:
                return original(*args, **kwargs)
            gui = threading.get_ident() == controller._ui_thread
            # Direct global effects remain backwards compatible, explicitly
            # unattributed. Their details never query a live QWidget on workers.
            scope = current.details() if current is not None else {
                "ownership": "legacy_global_unattributed" if global_probe else "monitored_editor",
                "scope": "legacy_global" if global_probe else "gui_adapter",
                "run_generation": controller._run_serial}
            token = current or _MonitorToken(reference, generation, epoch, tuple(scope.items()))
            timed_label = owned_label if owned_label and scope.get("ownership") == "monitored_editor" else label
            details = (controller._details(name, args, kwargs) if gui and not global_probe
                       else {**controller._render_details(args, kwargs, name),
                             "thread_id": threading.get_ident(), "thread": "gui" if gui else "worker"})
            details.update(scope, capture_epoch=token.epoch)
            paint_boundaries = ()
            font_before = None
            receipt_begin = None
            previous_receipt = controller._current_input_receipt
            if gui and not global_probe:
                if owner is controller.canvas:
                    if name in {'set_tool', 'set_selection', 'set_document'}:
                        controller._text_presentation_wait = None
                    elif name == '_change_selected_text_property':
                        key = args[0] if args else kwargs.get('key')
                        identifier = controller.canvas.selected_id
                        if key == 'font_size' and controller._text_owner(identifier) is not None:
                            font_before = identifier, controller.canvas.command_stack.revision
                if owner is controller.canvas and name in {'set_tool', 'set_selection', 'set_document',
                        '_begin_stroke', '_cancel_native_raster_stroke', '_cancel_mask_stroke',
                        '_cancel_paint_brush', '_restore_history_state'}:
                    if controller._feedback_contact_wait is not None:
                        controller._cancel_receipt(controller._feedback_contact_wait.get('receipt'), name)
                    controller._feedback_contact_wait = None
                    if name != '_begin_stroke' and previous_receipt is None:
                        controller._clear_input_receipts(name)
                if owner is controller.canvas and name in {'_begin_stroke', '_begin_paint_brush',
                        '_start_deferred_raster_stroke', '_start_deferred_paint_brush'}:
                    receipt_begin = controller._receipt_begin(name, token.epoch)
                    controller._current_input_receipt = receipt_begin[0]
                if name in {'_begin_stroke', '_end_stroke', '_begin_stroke_impl', '_end_stroke_impl'}:
                    controller._mark_contact_frame({
                        '_begin_stroke': 'after_begin', '_end_stroke': 'after_release',
                        '_begin_stroke_impl': 'after_admission', '_end_stroke_impl': 'after_commit'}[name],
                        token.epoch)
                elif name == 'paintEvent':
                    if controller._critical_paint_epoch == token.epoch:
                        paint_boundaries = tuple(sorted(controller._critical_paint_boundaries))
                    controller._critical_paint_boundaries.clear()
                    if paint_boundaries:
                        details['contact_frame_boundary'] = list(paint_boundaries)
            if transition and gui:
                controller.recorder.update_context(controller.context())
                controller.recorder.record_event(label + ".begin", category="transition",
                    details=details, capture_epoch=token.epoch)
            cpu_clock = getattr(time, 'thread_time', None) if cpu and gui else None
            started_cpu = cpu_clock() if cpu_clock is not None else None
            feedback_paint = (dict(epoch=token.epoch, batch=None)
                if gui and owner is controller.canvas and name == 'paintEvent' else None)
            previous_feedback_paint = controller._active_feedback_paint
            if feedback_paint is not None:
                controller._active_feedback_paint = feedback_paint
            started_wall = time.perf_counter() if feedback_paint is not None else None
            raised = None
            try:
                with controller._owned_scope(token), controller.recorder.measure(
                        timed_label, category=category, details=details, capture_epoch=token.epoch,
                        critical=critical or bool(paint_boundaries)):
                    result = original(*args, **kwargs)
            except BaseException as error:
                raised = type(error).__name__
                raise
            finally:
                elapsed_cpu = (cpu_clock() - started_cpu) * 1000. if started_cpu is not None else None
                if started_cpu is not None:
                    final = {**details, 'clock': 'thread_cpu'}
                    if raised is not None:
                        final['exception_type'] = raised
                    controller.recorder.record_event(timed_label + '.thread_cpu',
                        elapsed_cpu, category='gui_cpu',
                        details=final, capture_epoch=token.epoch,
                        critical=critical or bool(paint_boundaries))
                if feedback_paint is not None:
                    try:
                        if raised is None:
                            controller._finish_feedback_paint(feedback_paint,
                                (time.perf_counter()-started_wall)*1000., elapsed_cpu, details)
                            controller._finish_text_paint(token.epoch,
                                (time.perf_counter()-started_wall)*1000., elapsed_cpu, details)
                    finally:
                        controller._active_feedback_paint = previous_feedback_paint
                if receipt_begin is not None:
                    try:
                        if raised is None and not name.startswith('_start_deferred_'):
                            controller._bind_input_receipt(*receipt_begin)
                            if name == '_begin_paint_brush':
                                controller._arm_feedback_contact(token.epoch)
                        elif raised is not None:
                            controller._cancel_receipt(receipt_begin[0], raised)
                    finally:
                        controller._current_input_receipt = previous_receipt
                if gui and owner is controller.canvas and name == '_begin_stroke_impl' and raised is None:
                    controller._arm_feedback_contact(token.epoch)
            if gui and owner is controller.canvas:
                controller._observe_text_action(name, args, kwargs, result, token.epoch, font_before)
            if gui and name == 'filtered_batch':
                controller._observe_feedback_batch(owner, args, result)
            if paint_boundaries:
                # A finished Qt paint is not proof of new visible source ink.
                # Retain its actual pending/coverage outcome beside the span.
                presentation = getattr(controller.canvas, '_document_presentation_stats', None)
                controller.recorder.record_event('canvas.contact_frame.completed', category='frame',
                    details={**details,
                        'presentation_backend': str(getattr(presentation, 'backend', ''))[:32],
                        'frame_pending': bool(getattr(controller.canvas, '_projection_frame_pending', False)),
                        'gpu_pending': bool(getattr(controller.canvas, '_gpu_presentation_pending', False)),
                        'presentation_replayed': str(getattr(presentation, 'backend', '')) == 'gpu-pending',
                        'feedback_contact_covered': bool(getattr(controller.canvas, '_raster_feedback_contact_covered', False)),
                        'presented_revision': getattr(controller.canvas, '_projection_presented_revision', -1)},
                    capture_epoch=token.epoch, critical=True)
            if cache:
                controller.recorder.record_event(label + (".miss" if result is None else ".hit"),
                    category="cache", details=details, capture_epoch=token.epoch)
            if transition and gui:
                controller.recorder.update_context(controller.context())
                controller.recorder.record_event(label + ".end", category="transition",
                    details={**details, "accepted": result is not False}, capture_epoch=token.epoch)
            return result

        setattr(owner, name, measured)
        self._patches.append((owner, name, own, measured))

    def _install(self):
        canvas = self.canvas
        for name in ("_activate_tool", "_activate_named_tool", "_sync_tool_buttons",
                     "_sync_contextual_ribbon", "_refresh_hierarchy", "_refresh_masks_panel"):
            self._patch(self.window, name, "window." + name.lstrip("_"),
                        transition=name in {"_activate_tool", "_activate_named_tool"})
        self._patch(self.window, "_undo", "window.undo", cpu=True, critical=True)
        self._patch(canvas.command_stack, "undo", "history.undo", cpu=True, critical=True)
        self._patch(canvas.command_stack, "_apply", "history.apply", cpu=True, critical=True)
        self._patch(self.window, '_forward_popup_tablet_event', 'window.popup_tablet', cpu=True)
        chrome = getattr(self.window, 'window_chrome', None)
        if chrome is not None:
            self._patch(chrome, 'native_event', 'window.native_hit_test', cpu=True)
        for name in ("set_tool", "set_selection", "set_solo_entities", "set_document"):
            self._patch(canvas, name, "canvas." + name, transition=True)
        phases = (
            'tabletEvent', '_update_interaction_cursor',
            '_begin_paint_brush', '_start_deferred_raster_stroke', '_start_deferred_paint_brush',
            "paintEvent", "_paint_canvas_frame", "_paint_ready_document_projection", "_ensure_scene_cache", "_render_scene_cache_rect", "render_preview",
            "_paint_document_projection", "_collect_document_projection",
            "_render_document_tiles", "_render_document_region", "_show_on_top_plan",
            "_draw_predictive_ink", "_draw_live_vector_gesture",
            "_draw_selection", "_draw_focal_modifier_handles", "_flush_visual_dirty", "_paint_projection_grid",
            "_render_modified_layer", "_render_modified_object", "_render_mirror_target",
            "_modifier_layer_signature", "_modifier_object_signature", "_modifier_mask_fields",
            "_render_compound_layer_contents", "_compound_outline_mesh", "layer_effective_path",
            "_begin_stroke", "_continue_stroke", "_end_stroke", "_tool_press", "_tool_move", "_tool_release",
            "_begin_stroke_admitted", "_end_stroke_admitted",
            "_begin_stroke_impl", "_end_stroke_impl",
            "_cancel_native_raster_stroke", "_cancel_mask_stroke", "_cancel_paint_brush",
            "_begin_vector_gesture", "_continue_vector_gesture", "_end_vector_gesture",
            "_update_vector_anchor_drag", "_update_geometry_transform_preview",
            "_commit_raster_selection_transform", "begin_cage_tool", "_cage_object_preview",
            "_begin_drawing_selection_transform", "_prepare_multi_raster_selection",
            "hit_test_entities", "hit_test_objects", "_hit_vector_strokes",
            "sample_composited_color", "_sample_eyedropper", "push_model_change",
            "_push_vector_change", "_restore_history_state", "_invalidate_scene_cache",
            "_change_selected_text_property", "_publish_change_set",
        )
        for name in phases:
            self._patch(canvas, name, "canvas." + name.lstrip("_"),
                cpu=name in {'paintEvent', 'tabletEvent', '_update_interaction_cursor',
                            '_begin_stroke_admitted', '_end_stroke_admitted',
                            '_begin_stroke_impl', '_end_stroke_impl',
                            '_begin_drawing_selection_transform', '_prepare_multi_raster_selection'},
                critical=name in {'_begin_stroke', '_end_stroke', '_begin_stroke_admitted',
                                  '_end_stroke_admitted', '_begin_stroke_impl', '_end_stroke_impl'})
        for name in ("_modifier_cache_get", "_modifier_source_cache_get"):
            self._patch(canvas, name, "cache." + name.lstrip("_"), cache=True)
        preview = getattr(self.window, "preview", None)
        if preview is not None:
            self._patch(preview, "_render_live_preview", "navigator.render")
            navigator = getattr(preview, '_navigator_jobs', None)
            if navigator is not None:
                self._patch(navigator, 'request', 'navigator.request')
                self._patch(navigator.scheduler, 'poll', 'navigator.publish')
                self._patch(navigator.scheduler, 'submit', 'navigator.submit')
        scene = getattr(canvas, '_scene_controller', None)
        if scene is not None:
            self._patch(scene, 'request', 'scene.request')
            self._patch(scene, 'filtered_batch', 'scene.filtered_batch')
            self._patch(scene.scheduler, 'poll', 'scene.publish')
            self._patch(scene.scheduler, 'submit', 'scene.submit')
            self._patch(canvas._scene_snapshot_compiler, 'capture', 'scene.capture_start')
        # Late patches of timer-bound advance() do not replace the Qt slot.
        # Its dynamic lazy-capture/poll callees remain observable without
        # reconnecting timers or scheduling an extra render.
        from comic_editor.render.scene import SceneCapture, DetachedSceneBackend
        from comic_editor.render.scheduler import SceneScheduler
        from comic_editor.render.service import DocumentRenderService
        from comic_editor.render.scene_kernels import SceneKernels
        from comic_editor.render import effect_pipeline, scene_kernels, tile_effects
        from comic_editor.ui import interactive_effects
        from comic_editor.ui.document_presentation import GpuTilePresenter
        for name in ('draw_groups', '_save_state', '_upload', '_restore_state'):
            # These probes inherit only an owned editor scope. They do
            # not sample another window's presenter or inspect a live widget
            # from detached rendering workers.
            self._patch_owned(GpuTilePresenter, name, 'presentation.' + name.lstrip('_'))
        # Keep the native presenter separate from Qt's painter resume and
        # decorations. These owned probes add no driver/pixel queries and
        # disappear completely when the monitor stops.
        from PySide6.QtGui import QPainter
        from comic_editor.render.projection import DocumentProjection
        from comic_editor.ui import document_presentation, document_projection_features, raster_feedback
        for name, label in (('beginNativePainting', 'presentation.begin_native_painting'),
                            ('endNativePainting', 'presentation.end_native_painting')):
            self._patch_owned(QPainter, name, label, start_scope=self._canvas_painter_scope)
        for module in (document_presentation, document_projection_features):
            for name, label in (('draw_document_tile_groups', 'presentation.tile_groups'),
                                ('draw_document_border', 'presentation.document_border')):
                # The projection bridge retains imported function aliases.
                scope = self._border_painter_scope if name == 'draw_document_border' else self._canvas_painter_scope
                self._patch_owned(module, name, label, start_scope=scope,
                                  text_draw=name == 'draw_document_tile_groups')
        self._patch_owned(document_presentation, '_border_patches',
            'presentation.border_construction', start_scope=self._border_construction_scope, cpu=True)
        self._patch_owned(document_presentation, '_draw_document_border_patches',
            'presentation.border_draw_patches', start_scope=self._canvas_painter_scope)
        self._patch_owned(raster_feedback, 'present_pending_raster_gesture',
            'presentation.pending_gesture', start_scope=self._pending_painter_scope)
        self._patch_owned(DocumentProjection, '_trim', 'presentation.projection_trim',
            start_scope=self._projection_trim_scope)
        schedulers = []
        if scene is not None:
            schedulers.append((weakref.ref(scene.scheduler), "canvas"))
        navigator = getattr(preview, "_navigator_jobs", None) if preview is not None else None
        if navigator is not None:
            schedulers.append((weakref.ref(navigator.scheduler), "navigator"))
        self._owned_schedulers = tuple(schedulers)
        self._owned_capture_adapters = tuple((weakref.ref(adapter), role)
            for adapter, role in ((scene, "canvas"), (navigator, "navigator")) if adapter is not None)
        self._patch_owned(SceneCapture, "advance", "scene.capture_slice", start_scope=self._capture_scope,
            scope_labels={'gui_cage_capture_slice': 'cage.capture_slice'},
            cpu_scopes=('gui_capture_slice', 'gui_cage_capture_slice'), critical_min_ms=20.)
        self._patch_owned(SceneScheduler, "_evaluate", "scene.worker_envelope", start_scope=self._scheduler_scope, cpu=True)
        self._patch_owned(SceneScheduler, "_evaluate_admitted", "scene.worker_admitted",
                          start_scope=self._scheduler_scope, cpu=True, require_scope=True)
        from comic_editor.ui import tile_input, scene_consumers
        self._patch_owned(tile_input.TileInputGate, 'advance', 'input.gate_advance',
            start_scope=self._input_gate_scope, cpu=True, timeline_min_ms=1.)
        self._patch_owned(tile_input.TileInputGate, 'publish', 'input.gate_publish',
            start_scope=self._input_gate_scope, cpu=True, critical=True)
        for name, label in (('_capture_job', 'input.capture_job'), ('_accept', 'input.accept')):
            self._patch_owned(scene_consumers.SceneConsumers, name, label,
                start_scope=self._auxiliary_consumer_scope, cpu=True, critical=True, register_cage=True,
                scope_labels={'gui_cage_consumer': 'cage.' + name.lstrip('_')},
                begin_scopes=('gui_cage_consumer',))
        self._patch_owned(scene_consumers.SceneConsumers, '_request', 'cage.request',
            start_scope=self._cage_request_scope, cpu=True, critical=True,
            begin_scopes=('gui_cage_request',))
        self._patch_owned(scene_consumers.SceneConsumers, '_submit', 'input.submit',
            start_scope=self._auxiliary_consumer_scope, cpu=True, critical=True, register_input=True,
            register_cage=True, scope_labels={'gui_cage_consumer': 'cage.submit'},
            begin_scopes=('gui_cage_consumer',))
        self._patch_owned(scene_consumers, 'compute_snapshot', 'input.worker_envelope',
            start_scope=self._input_worker_scope, cpu=True, critical=True,
            scope_labels={'detached_cage_worker': 'cage.worker_envelope'},
            begin_scopes=('detached_cage_worker',))
        for name in ('prepare_input_tiles', 'prepare_input_bounds'):
            self._patch_owned(tile_input, name, 'input.' + name,
                start_scope=self._input_prepare_scope, cpu=True, critical=True)
        from comic_editor.render.input_sources import InputSourceCapture
        self._patch_owned(InputSourceCapture, 'advance', 'input.capture_slice',
            start_scope=self._input_capture_scope, cpu=True, timeline_min_ms=1.)
        for name in ("render_region", "render_tiles"):
            self._patch_owned(DocumentRenderService, name, "scene." + name)
        # capture() is a contextmanager constructor; ordinary paint spans
        # measure its actual body instead of reporting generator creation.
        self._patch_owned(DetachedSceneBackend, "paint", "scene.backend_paint")
        self._patch_owned(DetachedSceneBackend, "__init__", "scene.backend_init", cpu=True)
        for name in ("_render_modified_object", "_render_modified_layer"):
            self._patch_owned(SceneKernels, name, "scene." + name.lstrip("_"))
        # Large native owners can spend time establishing exact dependencies
        # before entering any cache lookup or effect. Observe those boundaries
        # only inside the current editor's immutable ownership token; never
        # serialize a semantic key, source path, or detached scene contents.
        for name in ("_modifier_object_signature", "_modifier_layer_signature"):
            self._patch_owned(SceneKernels, name, "scene." + name.lstrip("_"),
                cpu=True, timeline_min_ms=1.)
        from comic_editor.ui.cache_dependencies import RenderDependencies
        from comic_editor.core.tile_backing import PrefetchedPins, SnapshotBacking
        from comic_editor.render.cache import PersistentRenderCache
        from comic_editor.ui.disk_cache import DiskCacheController
        self._patch_owned(DiskCacheController, 'bind', 'disk_cache.bind',
            start_scope=self._cache_bind_scope)
        for name in ("tiles", "file_digest"):
            self._patch_owned(RenderDependencies, name, "scene.dependencies." + name,
                cpu=True, timeline_min_ms=1.)
        for owner, label in ((PrefetchedPins, "scene.source_pin"),
                             (SnapshotBacking, "scene.backing_pin")):
            self._patch_owned(owner, "pin", label, cpu=True, timeline_min_ms=1.)
        self._patch_owned(PersistentRenderCache, '__init__', 'scene.disk_cache_init',
            cpu=True, register_cache=True)
        self._patch_owned(PersistentRenderCache, '_committed_index',
            'scene.disk_cache_committed_index', cpu=True)
        self._patch_owned(PersistentRenderCache, '_reload_index',
            'scene.disk_cache_reload_index', start_scope=self._cache_reload_scope, cpu=True, critical=True)
        # Self-observation remains opt-in and exact-owner filtered. Separate
        # snapshot/encoding/disk boundaries reveal diagnostic GIL/API overhead
        # without changing recorder cadence, JSON semantics or worker behavior.
        self._patch_owned(ProcessResourceSampler, 'sample', 'monitor.resource_sample',
            start_scope=self._resource_sample_scope, cpu=True)
        self._patch_owned(type(self), '_metrics', 'monitor.metrics_collect',
            start_scope=self._monitor_metrics_scope, cpu=True)
        from comic_editor.ui.performance_monitor_dialog import PerformanceMonitorDialog
        self._patch_owned(PerformanceMonitorDialog, '_refresh', 'monitor.display_refresh',
            start_scope=self._monitor_display_scope, cpu=True, mask_unowned_scope=True)
        self._patch_owned(type(self), 'snapshot_for_display', 'monitor.display_snapshot',
            start_scope=self._monitor_display_snapshot_scope, cpu=True)
        self._patch_owned(PerformanceLogWriter, '_save', 'monitor.log_save',
            start_scope=self._log_writer_scope, cpu=True, mask_unowned_scope=True)
        self._patch_owned(type(self), 'snapshot', 'monitor.log_snapshot',
            start_scope=self._log_snapshot_scope, cpu=True)
        self._patch_owned(type(self), '_snapshot_for_log', 'monitor.log_snapshot',
            start_scope=self._log_snapshot_scope, cpu=True)
        from comic_editor.core import performance_log
        facade = _LogJsonFacade(performance_log.json)
        previous_json = performance_log.json
        performance_log.json = facade
        self._patches.append((performance_log, 'json', previous_json, facade))
        self._patch_owned(facade, 'dumps', 'monitor.log_serialize',
            start_scope=self._log_serialize_scope, cpu=True)
        self._patch_owned(performance_log, 'render_performance_summary', 'monitor.log_summary',
            start_scope=self._log_serialize_scope, cpu=True)
        self._patch_owned(PerformanceLogWriter, '_atomic_write', 'monitor.log_write',
            start_scope=self._log_write_scope, cpu=True)
        for name in ("_modifier_cache_get", "_modifier_source_cache_get"):
            self._patch_owned(SceneKernels, name, "scene.cache." + name.lstrip("_"), cache=True)
        self._patch_owned(effect_pipeline, "render_stages", "scene.effects.stages")
        # Scene kernels retained this separate import before monitor enable.
        self._patch_owned(scene_kernels, "render_stages", "scene.effects.stages")
        # These aliases were imported before monitor enable; patch the actual
        # called attributes, while preserving the older global compatibility
        # probes and explicitly marking their otherwise-unattributed scope.
        self._patch_owned(effect_pipeline, "apply_modifier_stack", "scene.effects.stack")
        # Ordinary scoped stages use this separately prebound CPU alias.
        # Its owner-filtered wrapper inherits the current immutable worker token.
        # Generic object/layer effects call this ordinary stage adapter
        # directly; they need not enter the spatial render_stages pipeline.
        self._patch_owned(interactive_effects, "render_interactive_stack", "scene.effects.stages")
        self._patch_owned(interactive_effects, "apply_modifier_stack", "scene.effects.stack")
        # Tile graphs retain their own module-bound alias.
        self._patch_owned(tile_effects, "apply_modifier_stack", "scene.effects.stack")
        self._patch_owned(scene_kernels, "apply_opacity_mask", "scene.effects.opacity_mask")
        for attribute in ("selection_settings", "selection_common", "layer_settings",
                          "modifier_controls", "text_object_controls"):
            control = getattr(self.window, attribute, None)
            if control is not None:
                self._patch(control, "refresh", attribute + ".refresh")
        # Class functions cover lazily created GL renderers. Existing signal-bound
        # callbacks remain intact; enclosing spans plus sampling include their cost.
        from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer
        from comic_editor.ui import distort_rendering, modifier_rendering
        self._patch(GpuPatternRenderer, "render", "gpu.pattern_wall_time")
        for name in ("apply_modifier_stack", "apply_opacity_mask", "apply_pattern_modifier"):
            # The ui module is an alias of render.modifier_rendering.
            # One source wrapper covers invocation-time pointwise/mask imports
            # and preserves explicitly unattributed direct legacy calls.
            owned_label = {"apply_modifier_stack": "scene.effects.stack",
                           "apply_opacity_mask": "scene.effects.opacity_mask"}.get(name)
            self._patch(modifier_rendering, name, "effects." + name, owned_label=owned_label)
        # Distort's worker closure imports this function when its stage is
        # requested. Patch the source module so both GUI and worker work is
        # observed without instrumenting per-strip or per-pixel inner loops.
        self._patch(distort_rendering, "render_distort", "effects.render_distort")
        for name in ("_outline_qimage", "_outline_stack_qimage", "_outline_effect"):
            self._patch(modifier_rendering, name, "effects." + name.lstrip("_"))
        for name in ("documentChanged", "visualChanged", "hierarchyChanged", "toolChanged",
                     "selectionChanged", "soloChanged", "interactionFinished", "cameraChanged"):
            signal = getattr(canvas, name)

            def observe(*args, signal_name=name):
                if not self.enabled:
                    return
                if signal_name in {'cameraChanged', 'hierarchyChanged', 'toolChanged',
                        'selectionChanged', 'soloChanged'}:
                    self._clear_input_receipts(signal_name)
                    self._feedback_contact_wait = None
                self._counts[signal_name] += 1
                if signal_name in {"toolChanged", "selectionChanged", "soloChanged", "interactionFinished"}:
                    self.recorder.update_context(self.context())
                    self.recorder.record_event("signal." + signal_name, category="signal")

            signal.connect(observe)
            self._connections.append((signal, observe))
        app = QApplication.instance()
        app.installEventFilter(self)
        self._filter_installed = True

    def start(self):
        global _active_controller
        if self._disposed:
            raise RuntimeError("The performance monitor has been deleted")
        if self.enabled:
            return
        previous = _active_controller() if _active_controller else None
        if previous is not None and previous is not self:
            previous.stop()
        _active_controller = weakref.ref(self)
        self._capture_generation = object()
        self._run_serial += 1
        self.recorder.clear()
        self._counts.clear()
        self._critical_paint_boundaries.clear()
        self._critical_paint_epoch = None
        self._feedback_contact_wait = None
        self._active_feedback_paint = None
        self._text_presentation_wait = None
        self._last_refresh = 0.0
        # An earlier writer can still be finishing a slow disk write. stop()
        # already detached its final snapshot; never attach it to this new run.
        self._writer = None
        self._writer_finalized = False
        try:
            self._resources = ProcessResourceSampler()
            self.recorder.start(self.context())
            self._register_monitor_object(self._resources, 'gui_monitor_resource')
            dialog = getattr(self.window, '_performance_monitor_dialog', None)
            if dialog is not None and dialog.controller is self:
                self._register_monitor_object(dialog, 'gui_monitor_display')
            self.canvas._performance.enabled = True
            for values in (self.canvas._performance.input_ms, self.canvas._performance.submit_ms,
                           self.canvas._performance.frame_ms):
                values.clear()
            self._install()
            self._editor_metrics = self._metrics()
            self._writer = PerformanceLogWriter(self._snapshot_for_log, self.log_directory)
            self._register_monitor_object(self._writer, 'detached_monitor_log')
            self._writer.start()
            self.last_log_path = self._writer.json_path
            self.log_status = ("Log save failed: " + self._writer.error if self._writer.error else
                               "Saving this run automatically: " + str(self.last_log_path))
            self.timer.start()
            self._tick()
        except Exception as error:
            self.stop()
            self.log_status = "Monitoring could not start: " + str(error)[:200]
            self.changed.emit()
            raise
        self.changed.emit()

    def _detach_instrumentation(self, *, qt_alive=True):
        """Restore Python patches even when Qt is already destroying widgets."""
        global _active_controller
        self._clear_input_receipts('monitor_stopped')
        self._capture_generation = None
        self._owned_schedulers = ()
        self._owned_capture_adapters = ()
        self._critical_paint_boundaries.clear()
        self._critical_paint_epoch = None
        self._feedback_contact_wait = None
        self._active_feedback_paint = None
        self._text_presentation_wait = None
        with self._cache_backings_lock:
            self._owned_cache_backings.clear()
            self._owned_input_jobs.clear()
            self._owned_cage_jobs.clear()
            self._owned_monitor_objects.clear()
        if _active_controller and _active_controller() is self:
            _active_controller = None
        if qt_alive:
            try:
                self.timer.stop()
                if self._filter_installed:
                    app = QApplication.instance()
                    if app is not None:
                        app.removeEventFilter(self)
            except RuntimeError:
                # Parent destruction can have disposed our QTimer already.
                pass
        # Qt automatically removes an event filter when the filter is destroyed.
        self._filter_installed = False
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()
        for owner, name, previous, wrapper in reversed(self._patches):
            try:
                if getattr(owner, name, None) is wrapper:
                    if previous is _MISSING:
                        delattr(owner, name)
                    else:
                        setattr(owner, name, previous)
            except (RuntimeError, AttributeError):
                # A destroyed QObject's attributes may be unavailable. Continue
                # so live class/module functions never retain our wrappers.
                pass
        self._patches.clear()
        try:
            self.canvas._performance.enabled = False
        except (RuntimeError, AttributeError):
            pass

    def _finalize_capture(self):
        """Use cached Python data only; safe after the Qt owner is destroyed."""
        self.recorder.stop()
        if self._writer is not None and not self._writer_finalized:
            try:
                # A slow final disk write must never observe the next run's data.
                final_snapshot = self._snapshot_for_log()
                self._writer.snapshot_provider = lambda: final_snapshot
                self._writer_finalized = True
            finally:
                self._writer.stop()
            self.last_log_path = self._writer.json_path
            self.log_status = ("Log save failed: " + self._writer.error if self._writer.error else
                               "Run saved: " + str(self.last_log_path))
        self._resources = None

    def stop(self):
        global _active_controller
        if _active_controller and _active_controller() is self:
            _active_controller = None
        if self._disposed:
            return
        if not self.enabled and not self._patches and not self._filter_installed:
            self._resources = None
            return
        self._detach_instrumentation()
        try:
            self._editor_metrics = self._metrics()
        except Exception as error:
            self.recorder.record_event("monitor.metrics_error", category="diagnostic",
                                       details={"error": str(error)[:200]})
        finally:
            self._finalize_capture()
        self.changed.emit()

    def _shutdown_workers(self):
        # Unlike closeEvent, QObject destruction can happen after the canvas's
        # C++ object is gone. No metrics reads, widget calls or changed signal.
        self._disposed = True
        self._detach_instrumentation(qt_alive=False)
        self._finalize_capture()

    def clear(self):
        # Preserve every enabled run on disk before clearing its displayed data.
        was_enabled = self.enabled
        self.stop()
        self.recorder.clear()
        self._counts.clear()
        if was_enabled:
            self.start()
        self.changed.emit()

    def snapshot_for_display(self, *, tab_index=None):
        # A display view uses fresh ordinary recorder data without detaching
        # unused graphs. None preserves the limited full-report API. Automatic
        # logs and exports call snapshot() and always retain every section.
        sections = {
            0: frozenset(('summary', 'active_spans')),
            1: frozenset(('timeline',)),
            2: frozenset(('stalls', 'stack_hotspots')),
            3: frozenset(('context', 'active_spans', 'recent_transitions',
                          'critical_events', 'metrics')),
        }.get(tab_index)
        return self.snapshot(timeline_limit=100, sections=sections)

    def snapshot(self, *, timeline_limit=None, sections=None):
        """Thread safe: writer uses cached detached metrics, never Qt objects."""
        data = self.recorder.snapshot(timeline_limit=timeline_limit, sections=sections)
        return self._snapshot_metadata(data)

    def _snapshot_for_log(self):
        # Only our read-only checkpoint writer consumes these immutable
        # published records. UI/export callers keep fully detached snapshots.
        return self._snapshot_metadata(self.recorder._snapshot_for_log())

    def _snapshot_metadata(self, data):
        data["editor_metrics"] = self._editor_metrics
        data["instrumentation"] = {
            "timings": "Inclusive wall time; nested spans overlap. Not GPU execution or presentation time.",
            "sampling": "GUI Python stack; native calls may hold the GIL and delay the sampler.",
            "content": "Entity IDs and structural metadata only; no artwork, text content or local variables.",
            "already_connected_slots": "Existing Qt-bound callbacks remain connected; dynamic capture/poll/frame callees are timed, not every outer slot.",
            "detached_workers": "Owned scheduler and GUI-bound cache IO spans use actual thread IDs and frozen ownership metadata; inclusive wall time includes admission and waits; separate thread_cpu spans use time.thread_time. Worker stacks are not sampled.",
            "ownership": "Detached scene worker/capture and GUI-bound cache IO spans are filtered to the monitored editor, run generation and recorder epoch. Legacy global effect probes are labelled unattributed when outside an owned scope.",
            "measurement_boundary": "Tasks submitted before enable may retain the original worker entry and are unobserved; no retroactive reconstruction.",
            "cache_metrics": "Legacy canvas pools only; the GUI never traverses live detached-worker cache dictionaries.",
        }
        return data

    def _tick(self):
        if not self.enabled:
            return
        self.recorder.heartbeat()
        now = time.perf_counter()
        if now - self._last_refresh >= 1.0:
            self._last_refresh = now
            try:
                self.recorder.update_context(self.context())
                self._editor_metrics = self._metrics()
                self.recorder.record_metrics(self._editor_metrics)
            except Exception as error:
                self.recorder.record_event("monitor.metrics_error", category="diagnostic",
                                           details={"error": str(error)[:200]})
            if self._writer and self._writer.error:
                self.log_status = "Log save failed: " + self._writer.error
            self.changed.emit()

    def eventFilter(self, watched, event):  # noqa: N802
        if (self.enabled and event.type() == QEvent.DeferredDelete
                and (watched is self or watched is self.window)):
            self.stop()
            return False
        if (self.enabled and event.type() == QEvent.FocusIn and watched.isWidgetType()
                and (watched is self.window or self.window.isAncestorOf(watched))):
            focus = self._focus_metrics()
            self._editor_metrics = {**self._editor_metrics, 'focus': focus}
            self.recorder.update_context(self.context())
            self.recorder.record_event('focus.changed', category='input', details=focus)
        if self.enabled and watched is self.canvas:
            names = {QEvent.MouseButtonPress: "mouse.press", QEvent.MouseButtonRelease: "mouse.release",
                     QEvent.MouseMove: "mouse.move", QEvent.TabletPress: "tablet.press",
                     QEvent.TabletMove: "tablet.move", QEvent.TabletRelease: "tablet.release",
                     QEvent.Wheel: "wheel", QEvent.Paint: "paint.request"}
            name = names.get(event.type())
            if name:
                self._counts[name] += 1
                if event.type() in {QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                                    QEvent.TabletPress, QEvent.TabletRelease}:
                    pen = event.type() in {QEvent.TabletPress, QEvent.TabletRelease}
                    held_button_transition = (pen and getattr(self.canvas, '_pen_contact_active', False)
                        and getattr(self.canvas, '_tablet_tool_active', False)
                        and hasattr(event, 'pressure') and event.pressure() > 0.
                        and event.buttons() != Qt.NoButton)
                    if event.type() in {QEvent.MouseButtonPress, QEvent.TabletPress} and not held_button_transition:
                        self._cancel_receipt(self._input_receipt, 'next_press')
                        self._input_receipt_serial += 1
                        self._input_receipt = {'received': time.perf_counter(),
                            'epoch': self.recorder.capture_epoch, 'generation': self._capture_generation,
                            'chapter': id(self.canvas.chapter), 'tiles': id(self.canvas.tiles),
                            'source_id': self.canvas.selected_id, 'tool': _value(self.canvas.tool),
                            'input_kind': 'tablet' if pen else 'mouse',
                            'serial': self._input_receipt_serial, 'finished': False}
                    elif not held_button_transition:
                        self._cancel_receipt(self._input_receipt, 'released_without_admission')
                        self._input_receipt = None
                    self._mark_contact_frame('after_press' if event.type() in {
                        QEvent.MouseButtonPress, QEvent.TabletPress} else 'after_release',
                        self.recorder.capture_epoch)
                    self.recorder.update_context(self.context())
                    self.recorder.record_event(name, category="input", critical=True)
        return False
