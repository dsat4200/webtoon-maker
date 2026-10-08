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
from PySide6.QtCore import QEvent, QObject, QTimer, Signal, qVersion
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
        self._connections = []
        self._counts = Counter()
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
        }

    def _metrics(self):
        canvas = self.canvas
        jobs = canvas._effect_jobs
        gpu = getattr(canvas, "_gpu_pattern_renderer", None)
        data = {
            "runtime": {"pyside_version": pyside_version, "qt_version": qVersion(),
                        "qt_platform": QApplication.platformName()},
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

    def _patch_owned(self, owner, name, label, *, start_scope=None, cache=False, cpu=False,
                     require_scope=False):
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
                return original(*args, **kwargs)
            if require_scope:
                if (current is None or current.details().get("scope") != "detached_worker"
                        or any(current.details().get(key) != metadata.get(key) for key in
                               ("scheduler_role", "consumer", "demand_serial", "document_revision"))):
                    return original(*args, **kwargs)
                token = current
            else:
                token = (_MonitorToken(reference, generation, epoch, tuple(metadata.items()))
                         if metadata is not None else current)
            if token is None:
                return original(*args, **kwargs)
            if token.details().get("ownership") != "monitored_editor":
                return original(*args, **kwargs)
            details = {**token.details(), **controller._render_details(args, kwargs),
                       "capture_epoch": token.epoch, "thread_id": threading.get_ident(),
                       "thread": "gui" if threading.get_ident() == controller._ui_thread else "worker"}
            cpu_clock = getattr(time, "thread_time", None) if cpu else None
            started_cpu = cpu_clock() if cpu_clock is not None else None
            raised = None
            try:
                with controller._owned_scope(token), controller.recorder.measure(
                        label, details=details, capture_epoch=token.epoch):
                    result = original(*args, **kwargs)
            except BaseException as error:
                raised = type(error).__name__
                raise
            finally:
                if started_cpu is not None:
                    final = {**details, "clock": "thread_cpu"}
                    if raised is not None:
                        final["exception_type"] = raised
                    controller.recorder.record_event(label + ".thread_cpu",
                        (cpu_clock() - started_cpu) * 1000., category="worker_cpu",
                        details=final, capture_epoch=token.epoch)
            if cache:
                # Worker cache hit counters use the recorder's existing lock.
                controller.recorder.record_event(label + (".miss" if result is None else ".hit"),
                    category="cache", details=details, capture_epoch=token.epoch)
            return result

        setattr(owner, name, measured)
        self._patches.append((owner, name, own, measured))

    def _patch(self, owner, name, label, category="phase", *, transition=False, cache=False,
               owned_label=None):
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
            if transition and gui:
                controller.recorder.update_context(controller.context())
                controller.recorder.record_event(label + ".begin", category="transition",
                    details=details, capture_epoch=token.epoch)
            with controller._owned_scope(token), controller.recorder.measure(
                    timed_label, category=category, details=details, capture_epoch=token.epoch):
                result = original(*args, **kwargs)
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
        for name in ("set_tool", "set_selection", "set_solo_entities", "set_document"):
            self._patch(canvas, name, "canvas." + name, transition=True)
        phases = (
            "paintEvent", "_paint_canvas_frame", "_paint_ready_document_projection", "_ensure_scene_cache", "_render_scene_cache_rect", "render_preview",
            "_paint_document_projection", "_collect_document_projection",
            "_render_document_tiles", "_render_document_region", "_show_on_top_plan",
            "_draw_predictive_ink", "_draw_live_vector_gesture",
            "_draw_selection", "_draw_focal_modifier_handles", "_flush_visual_dirty",
            "_render_modified_layer", "_render_modified_object", "_render_mirror_target",
            "_modifier_layer_signature", "_modifier_object_signature", "_modifier_mask_fields",
            "_render_compound_layer_contents", "_compound_outline_mesh", "layer_effective_path",
            "_begin_stroke", "_continue_stroke", "_end_stroke", "_tool_press", "_tool_move", "_tool_release",
            "_begin_vector_gesture", "_continue_vector_gesture", "_end_vector_gesture",
            "_update_vector_anchor_drag", "_update_geometry_transform_preview",
            "_commit_raster_selection_transform", "begin_cage_tool", "_cage_object_preview",
            "hit_test_entities", "hit_test_objects", "_hit_vector_strokes",
            "sample_composited_color", "_sample_eyedropper", "push_model_change",
            "_push_vector_change", "_restore_history_state", "_invalidate_scene_cache",
        )
        for name in phases:
            self._patch(canvas, name, "canvas." + name.lstrip("_"))
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
        schedulers = []
        if scene is not None:
            schedulers.append((weakref.ref(scene.scheduler), "canvas"))
        navigator = getattr(preview, "_navigator_jobs", None) if preview is not None else None
        if navigator is not None:
            schedulers.append((weakref.ref(navigator.scheduler), "navigator"))
        self._owned_schedulers = tuple(schedulers)
        self._owned_capture_adapters = tuple((weakref.ref(adapter), role)
            for adapter, role in ((scene, "canvas"), (navigator, "navigator")) if adapter is not None)
        self._patch_owned(SceneCapture, "advance", "scene.capture_slice", start_scope=self._capture_scope)
        self._patch_owned(SceneScheduler, "_evaluate", "scene.worker_envelope", start_scope=self._scheduler_scope, cpu=True)
        self._patch_owned(SceneScheduler, "_evaluate_admitted", "scene.worker_admitted",
                          start_scope=self._scheduler_scope, cpu=True, require_scope=True)
        for name in ("render_region", "render_tiles"):
            self._patch_owned(DocumentRenderService, name, "scene." + name)
        # capture() is a contextmanager constructor; ordinary paint spans
        # measure its actual body instead of reporting generator creation.
        self._patch_owned(DetachedSceneBackend, "paint", "scene.backend_paint")
        for name in ("_render_modified_object", "_render_modified_layer"):
            self._patch_owned(SceneKernels, name, "scene." + name.lstrip("_"))
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
        self._last_refresh = 0.0
        # An earlier writer can still be finishing a slow disk write. stop()
        # already detached its final snapshot; never attach it to this new run.
        self._writer = None
        self._writer_finalized = False
        try:
            self._resources = ProcessResourceSampler()
            self.recorder.start(self.context())
            self.canvas._performance.enabled = True
            for values in (self.canvas._performance.input_ms, self.canvas._performance.submit_ms,
                           self.canvas._performance.frame_ms):
                values.clear()
            self._install()
            self._editor_metrics = self._metrics()
            self._writer = PerformanceLogWriter(self.snapshot, self.log_directory)
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
        self._capture_generation = None
        self._owned_schedulers = ()
        self._owned_capture_adapters = ()
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
                final_snapshot = self.snapshot()
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

    def snapshot_for_display(self):
        return self.snapshot(timeline_limit=100)

    def snapshot(self, *, timeline_limit=None):
        """Thread safe: writer uses cached detached metrics, never Qt objects."""
        data = self.recorder.snapshot(timeline_limit=timeline_limit)
        data["editor_metrics"] = self._editor_metrics
        data["instrumentation"] = {
            "timings": "Inclusive wall time; nested spans overlap. Not GPU execution or presentation time.",
            "sampling": "GUI Python stack; native calls may hold the GIL and delay the sampler.",
            "content": "Entity IDs and structural metadata only; no artwork, text content or local variables.",
            "already_connected_slots": "Existing Qt-bound callbacks remain connected; dynamic capture/poll/frame callees are timed, not every outer slot.",
            "detached_workers": "Owned scheduler spans use actual thread IDs and frozen demand metadata; inclusive wall time includes admission and waits; separate thread_cpu spans use time.thread_time. Worker stacks are not sampled.",
            "ownership": "Detached scene worker/capture spans are filtered to the monitored editor, run generation and recorder epoch. Legacy global effect probes are labelled unattributed when outside an owned scope.",
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
                    self.recorder.update_context(self.context())
                    self.recorder.record_event(name, category="input")
        return False
