"""Opt-in native Qt profiling of a saved project, with no project writes.

Run manually, not with pytest. Native Windows mode supports hidden raster and
OpenGL widgets, with the usual GPU effect kernels and no activated window.
Raster is the default; select --canvas gpu to measure actual GL presentation.
Offscreen mode uses CPU fallbacks. Sources are loaded directly and edited only
in memory.
Timings measure Python handlers and Qt paint work, not physical display latency.

Example:
    python tests/benchmark_real_project_interactions.py --project PROJECT \
        --label baseline --groups navigation,selection,solo --profile
"""
from __future__ import annotations

import argparse
import atexit
from collections import Counter
import cProfile
import faulthandler
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pstats
import statistics
import sys
import threading
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--chapter", default="0a72f08009294aa0a3d14e6a38e22bbb")
    parser.add_argument("--object", dest="object_id")
    parser.add_argument("--objects", help="Comma-separated IDs exercised independently, restoring after every edit")
    parser.add_argument("--source-root", type=Path,
                        help="Import editor code from an isolated before/after source snapshot")
    parser.add_argument("--activate-stack", action="store_true",
                        help="Unmute all selected target and ancestor effects in memory for a stress run, then undo")
    parser.add_argument("--output", type=Path,
                        default=ROOT / ".artifacts" / "night-performance-20261006")
    parser.add_argument("--label", default="real-project")
    parser.add_argument("--platform", choices=("windows", "offscreen"), default="windows")
    parser.add_argument("--canvas", choices=("raster", "gpu"), default="raster")
    parser.add_argument("--groups", default="navigation,selection,solo,modifier,transform,hierarchy,mask,shape,warp")
    parser.add_argument("--positions", default="300,6456,12000,27000,42000,54000")
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--settle-seconds", type=float, default=8.)
    parser.add_argument("--verify-seconds", type=float, default=60.,
                        help="Bounded final exact-convergence wait, separate from interaction phase timing")
    parser.add_argument("--scale", type=float, default=.75)
    parser.add_argument("--viewport", default="1000,800", help="Logical canvas width,height")
    parser.add_argument("--target-center-y", type=float,
                        help="Override target framing for a matched neighboring-object viewport")
    parser.add_argument("--parameter-mask-line",
                        help="World-space x1,y1,x2,y2 for the opt-in parameter-mask painted stroke")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-first-paint", action="store_true",
                        help="Profile only each phase's first render() call, excluding action/metadata/settlement; mutually exclusive with --profile")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--job-telemetry", action="store_true")
    parser.add_argument("--planning-telemetry", action="store_true",
                        help="Instrument bounded stage/region preparation counts and frame extents; not clean timing")
    parser.add_argument("--process-telemetry", action="store_true",
                        help="Read owning Windows process working-set/private counters at phase boundaries and diagnostic checkpoints; disabled for clean timing")
    parser.add_argument("--live-evidence", action="store_true",
                        help="Diagnostic existing INTERACTIVE request/status/hash evidence; hashes run outside timed first paints")
    parser.add_argument("--probe-modifier", help="Modifier ID for the optional parameter-probe group; defaults to selected target's Twirl")
    parser.add_argument("--probe-parameter", default="angle")
    parser.add_argument("--probe-value", type=float, default=55.86)
    parser.add_argument("--probe-wait-seconds", type=float, default=20.,
                        help="Untimed bounded feedback observation before parameter-probe undo")
    parser.add_argument("--probe-after-mask", action="store_true",
                        help="Insert parameter probe after new mask release, before painted exact verification; diagnostic only")
    parser.add_argument("--capture-distort-input", metavar="MODIFIER_ID",
                        help="Diagnostic only: pin one native exact render_distort input for this modifier and write raw bytes after timing/worker shutdown; disabled for clean captures")
    parser.add_argument("--phase-timeout", type=float, default=0.,
                        help="Diagnostic watchdog: save the blocked phase/threads/profile and exit after this many seconds")
    parser.add_argument("--transform-modes", default="translate,scale,warp",
                        help="Comma-separated transform modes; use scale for a focused commit reproducer")
    parser.add_argument("--navigator", action="store_true")
    parser.add_argument("--read-only-disk-cache-root", type=Path,
                        help="Opt-in ordinary persistent read-through/dependency binding; cache recording and all writes are forbidden")
    parser.add_argument("--navigator-wait-seconds", type=float, default=15.,
                        help="Bounded natural navigator recovery check after Escape in the history group")
    parser.add_argument("--save-frames", action="store_true")
    parser.add_argument("--verify-exact", action="store_true",
                        help="Compare final settled tiles with synchronous rendering through the same pipeline")
    parser.add_argument("--verify-active-exact", action="store_true",
                        help="Also verify a fully activated stress stack before undo restores the saved muted state")
    parser.add_argument("--verify-navigation-exact", action="store_true",
                        help="Verify each requested navigation viewport before moving on; requires --verify-exact")
    parser.add_argument("--verify-cold-reference", action="store_true",
                        help="After primary checks, compare three saved native patches to a separate fresh synchronous canvas with no derived caches; 120-second child bound")
    args = parser.parse_args()
    if args.profile and args.profile_first_paint:
        parser.error("--profile and --profile-first-paint require separate captures")
    if args.probe_after_mask and (not args.live_evidence or "parameter-mask" not in args.groups.split(",")):
        parser.error("--probe-after-mask requires --live-evidence and the parameter-mask group")
    if args.verify_active_exact and not args.activate_stack:
        parser.error("--verify-active-exact requires --activate-stack")
    if args.verify_cold_reference and not args.verify_exact:
        parser.error("--verify-cold-reference requires --verify-exact")
    if args.verify_navigation_exact and (not args.verify_exact or "navigation" not in args.groups.split(",")):
        parser.error("--verify-navigation-exact requires --verify-exact and the navigation group")
    if args.source_root:
        sys.path.insert(0, str(args.source_root.resolve()))
    os.environ["QT_QPA_PLATFORM"] = args.platform
    os.environ.setdefault("QT_TLS_BACKEND", "schannel")

    from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer
    from PySide6.QtGui import QImage, QKeyEvent, QPainterPath, QPolygonF, QSurfaceFormat
    from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

    from comic_editor.core.images import ImageStore
    from comic_editor.core.models import (
        BoundGeometry, ChapterDocument, ImageObject, ParameterMaskBinding,
        RasterObject, TextObject, ToneMask,
    )
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core import settings as settings_module
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui.canvas import GpuCanvasWidget, RasterCanvasWidget, ToolKind
    from comic_editor.ui.performance_monitor import PerformanceMonitorController
    from comic_editor.ui.preview import ChapterPreview
    from comic_editor.ui.modifier_controls import ModifierControls

    output = (args.output / args.label).resolve()
    if not output.is_relative_to(args.output.resolve()) or output == args.output.resolve():
        raise ValueError("The label must identify a child output folder")
    output.mkdir(parents=True, exist_ok=True)
    # Preserve the exact opt-in harness alongside each capture. Production
    # sources may be an archived Git state; later harness edits must not make
    # an earlier timing run difficult to reproduce.
    harness_source = Path(__file__).read_bytes()
    (output / "benchmark-harness.py").write_bytes(harness_source)
    production_root = args.source_root.resolve() if args.source_root else ROOT
    def source_manifest():
        return {str(path.relative_to(production_root)).replace("\\", "/"):
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((production_root / "comic_editor").rglob("*.py"))}
    production_manifest = source_manifest()
    (output / "source-manifest.json").write_text(json.dumps(production_manifest, indent=2), "utf-8")
    settings_module.settings_path = lambda: output / "isolated-settings.json"
    source = args.project.resolve() / "chapters" / args.chapter
    if not (source / "chapter.json").is_file():
        raise FileNotFoundError(source / "chapter.json")
    # Record the exact saved inputs consumed by the loader. This is outside
    # timed loading/interaction work and excludes recovery and derived caches.
    # Native tile hashes below can therefore be paired across source snapshots
    # without treating a PNG screenshot as an exact-output oracle.
    input_paths = [source / "chapter.json"]
    for folder in ("raster", "masks", "images"):
        input_paths.extend(path for path in (source / folder).rglob("*") if path.is_file())
    input_manifest = {str(path.relative_to(source)).replace("\\", "/"):
                      hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(input_paths)}
    (output / "input-source-manifest.json").write_text(json.dumps(input_manifest, indent=2), "utf-8")
    surface_format = QSurfaceFormat()
    surface_format.setVersion(3, 3)
    surface_format.setProfile(QSurfaceFormat.CoreProfile)
    surface_format.setSamples(0)
    surface_format.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(surface_format)
    app = QApplication.instance() or QApplication([])
    started = time.perf_counter()
    # Deliberately bypass SeriesRepository.load_chapter: its interrupted-save
    # recovery can write to the source. No save/recovery/cache APIs are called.
    chapter = ChapterDocument.from_dict(json.loads((source / "chapter.json").read_text("utf-8")))
    tiles = TileStore()
    tiles.load_directory(source / "raster", {
        key for key, obj in chapter.objects.items() if isinstance(obj, RasterObject)})
    tiles.load_directory(source / "masks", set(chapter.masks), clear=False)
    images = ImageStore()
    images.load_directory(source / "images", {
        key: (obj.source_filename, obj.source_mime_type)
        for key, obj in chapter.objects.items() if isinstance(obj, ImageObject)})
    load_ms = (time.perf_counter() - started) * 1000
    window = QWidget()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.setAttribute(Qt.WA_ShowWithoutActivating, True)
    viewport = tuple(int(value) for value in args.viewport.split(","))
    if len(viewport) != 2 or min(viewport) <= 0:
        raise ValueError("Viewport must contain two positive logical dimensions")
    window.resize(*viewport)
    layout = QHBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    settings = EditorSettings(canvas_renderer="auto" if args.platform == "windows" else "raster",
                              snap_to_grid=False, predictive_ink=False, grid_overlay_visible=False)
    canvas_type = GpuCanvasWidget if args.canvas == "gpu" else RasterCanvasWidget
    canvas = window.canvas = canvas_type(settings, window)
    layout.addWidget(canvas)
    canvas.set_document(chapter, tiles, images)
    disk_binding = None
    disk_binding_metadata = {"enabled": False, "method": "Memory-only canvas; no persistent dependency fingerprints"}
    if args.read_only_disk_cache_root is not None:
        from collections import OrderedDict
        from comic_editor.render.cache import PersistentRenderCache
        from comic_editor.ui.cache_dependencies import RenderDependencies
        from comic_editor.ui.disk_cache import DiskCacheController

        class ReadOnlyBacking(PersistentRenderCache):
            """Use production reads verbatim, with no durable mutation route."""
            def _assert_read_only(self):
                if self.recording or self.writes or self.staged or self.publication or self.saved:
                    raise AssertionError("The profiling backing attempted a cache write")

            def record(self):
                raise AssertionError("Cache recording is forbidden in the read-only profiling binding")

            def retain(self, *values, **kwargs):
                self._assert_read_only()
                # Production retain also returns immediately outside recording.
                # Block it explicitly so a later helper edit cannot admit writes.
                return None

            def clear(self, *values, **kwargs):
                raise AssertionError("Cache clearing is forbidden in the read-only profiling binding")

            def flush(self):
                raise AssertionError("Cache publication is forbidden in the read-only profiling binding")

            def drain(self):
                self._assert_read_only()

        class ReadOnlyDiskBinding:
            # Reuse the production scene keys, validity guards and read path.
            # No controller UI, recording/build timer or alternative renderer.
            tile_key = DiskCacheController.tile_key
            reusable = DiskCacheController.reusable
            lookup_tile = DiskCacheController.lookup_tile
            retain_tile = DiskCacheController.retain_tile
            observe = DiskCacheController.observe

            def __init__(self):
                self.canvas = canvas
                self.backing = ReadOnlyBacking(args.read_only_disk_cache_root.resolve(),
                    contract=chapter.pixel_contract.signature, environment=RenderDependencies.environment())
                self.dependencies = RenderDependencies(canvas, self.backing)
                self._keys = {}
                self.observed = OrderedDict()

        cache_index = args.read_only_disk_cache_root.resolve() / "index.json"
        cache_index_before = hashlib.sha256(cache_index.read_bytes()).hexdigest() if cache_index.is_file() else None
        def cache_content_manifest():
            root = args.read_only_disk_cache_root.resolve()
            return {str(path.relative_to(root)).replace("\\", "/"):
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(root.rglob("*")) if path.is_file()}
        cache_contents_before = cache_content_manifest()
        (output / "input-cache-manifest.json").write_text(json.dumps(cache_contents_before, indent=2), "utf-8")
        begin = time.perf_counter()
        disk_binding = ReadOnlyDiskBinding()
        blocked_routes = []
        for route in ("record", "clear", "flush"):
            try:
                getattr(disk_binding.backing, route)()
            except AssertionError:
                blocked_routes.append(route)
            else:
                raise AssertionError(f"Read-only backing failed to block {route}")
        disk_binding.backing.recording = 1
        try:
            try:
                disk_binding.backing.retain("probe", (), None)
            except AssertionError:
                blocked_routes.append("retain-while-recording")
            else:
                raise AssertionError("Read-only backing failed to block recording retention")
        finally:
            disk_binding.backing.recording = 0
        canvas._disk_cache_controller = disk_binding
        canvas._persistent_render_cache = disk_binding.backing
        canvas._render_dependencies = disk_binding.dependencies
        tiles.render_fingerprint = disk_binding.dependencies.tiles
        images.render_fingerprint = disk_binding.dependencies.image
        canvas._document_projection.backing_lookup = disk_binding.lookup_tile
        canvas._document_projection.backing_retain = disk_binding.retain_tile
        disk_binding_metadata = {
            "enabled": True, "root": str(args.read_only_disk_cache_root.resolve()),
            "method": "Production persistent read-through, dependencies and controller tile-key/lookup methods; all recording/write/clear/publication routes blocked; no MainWindow or cache-build timer",
            "bind_ms": round((time.perf_counter() - begin) * 1000, 3),
            "initial_entries": len(disk_binding.backing.entries),
            "index_sha256_before": cache_index_before,
            "write_block_checks": blocked_routes,
            "files_before": len(cache_contents_before),
            "content_manifest_sha256_before": hashlib.sha256(
                json.dumps(cache_contents_before, sort_keys=True).encode()).hexdigest(),
        }
    if args.navigator:
        window.preview = ChapterPreview(canvas, window)
        layout.addWidget(window.preview)
    canvas.center_x, canvas.center_y, canvas.scale = 540., 300., args.scale
    monitor = PerformanceMonitorController(window, log_directory=output / "monitor-logs")
    if args.monitor:
        monitor.start()
    canvas._performance.enabled = args.monitor or args.profile
    controls = ModifierControls(canvas, window)
    groups = set(args.groups.split(","))
    results = []
    profiles = []
    phase_prefix = ""
    job_telemetry = {"requests": 0, "active_same_key_requests": 0, "scope_replacements": 0,
                     "requests_for_completed_keys": 0, "cancels": 0, "events": [],
                     "peaks": {}, "cache_samples": [], "dropped_key_observations": 0}
    completed_keys = Counter()
    worker_pair_counts = Counter()
    worker_totals = {}
    repeated_keys = Counter()
    completed_semantic_keys = Counter()
    cancel_sites = Counter()
    retention_counts = Counter()
    telemetry_lock = threading.Lock()
    job_patches = []
    distort_capture = {"requested_modifier_id": args.capture_distort_input,
        "enabled": bool(args.capture_distort_input), "status": "waiting" if args.capture_distort_input else "disabled",
        "method": "One immutable QImage value-copy at the actual native exact render_distort compute input; raw original-format stride bytes written only after timed phases/profilers and worker shutdown. The handle pins source memory outside ordinary caches, so this is instrumented causal evidence, not clean performance.",
        "record": None}
    captured_distort_input = []
    distort_capture_lock = threading.Lock()
    if args.capture_distort_input:
        import copy
        from comic_editor.render.pixels import current_contract
        from comic_editor.ui import distort_pipeline, distort_rendering
        if args.capture_distort_input not in chapter.modifiers:
            raise ValueError("--capture-distort-input names an absent modifier")
        distort_context = threading.local()
        original_capture_stage = distort_pipeline.render_distort_stage
        def capture_stage(owner, image, base, bounds, target, modifier, placement,
                          fields, key, scope, provisional, navigator, **kwargs):
            previous = getattr(distort_context, "metadata", None)
            if modifier.modifier_id == args.capture_distort_input:
                distort_context.metadata = {"key": key, "scope": scope,
                    "exact": bool(getattr(owner, "_projection_exact", False)),
                    "provisional": bool(provisional), "navigator": bool(navigator),
                    "operation": payload.get("active_phase", {}).get("operation"),
                    "projection_revision": owner._document_projection.revision}
            try:
                return original_capture_stage(owner, image, base, bounds, target, modifier,
                    placement, fields, key, scope, provisional, navigator, **kwargs)
            finally:
                distort_context.metadata = previous
        job_patches.append((distort_pipeline, "render_distort_stage", original_capture_stage))
        distort_pipeline.render_distort_stage = capture_stage

        original_capture_request = canvas._effect_jobs.request
        def capture_request(scope, key, compute, *values, **kwargs):
            metadata = getattr(distort_context, "metadata", None)
            if metadata is not None:
                original_compute = compute
                def compute(*args, **options):
                    previous = getattr(distort_context, "metadata", None)
                    distort_context.metadata = metadata
                    try:
                        return original_compute(*args, **options)
                    finally:
                        distort_context.metadata = previous
            return original_capture_request(scope, key, compute, *values, **kwargs)
        job_patches.append((canvas._effect_jobs, "request", original_capture_request))
        canvas._effect_jobs.request = capture_request

        original_capture_distort = distort_rendering.render_distort
        def capture_distort(image, bounds, modifier, local_to_world=None,
                            output_bounds=None, cancelled=None, pixel_scale=1., **kwargs):
            context = getattr(distort_context, "metadata", None)
            if (modifier.modifier_id == args.capture_distort_input and context is not None
                    and context["exact"] and not context["provisional"] and not context["navigator"]
                    and pixel_scale == 1. and not image.isNull() and not bounds.isEmpty()):
                with distort_capture_lock:
                    if not captured_distort_input:
                        transform = local_to_world
                        captured_distort_input.append({"image": QImage(image), "key": context["key"],
                            "modifier": copy.deepcopy(modifier),
                            "scope": context["scope"], "record": {
                                "captured_t": time.perf_counter(), "thread_id": threading.get_ident(),
                                "operation": context["operation"], "projection_revision": context["projection_revision"],
                                "modifier_id": modifier.modifier_id, "modifier_type": modifier.modifier_type,
                                "native_size": [image.width(), image.height()],
                                "width": image.width(), "height": image.height(),
                                "format": image.format().name, "format_value": image.format().value,
                                "bytes_per_line": image.bytesPerLine(), "pinned_bytes": int(image.sizeInBytes()),
                                "source_qt_cache_key": int(image.cacheKey()), "bounds": list(bounds.getRect()),
                                "output_bounds": list(output_bounds.getRect()) if output_bounds is not None else None,
                                "mapping": [getattr(transform, name)() for name in
                                    ("m11", "m12", "m13", "m21", "m22", "m23", "m31", "m32", "m33")]
                                    if transform is not None else [1., 0., 0., 0., 1., 0., 0., 0., 1.],
                                "pixel_scale": pixel_scale, "pixel_contract": current_contract().signature,
                                "exact": context["exact"], "provisional": context["provisional"],
                                "navigator": context["navigator"]}})
            return original_capture_distort(image, bounds, modifier, local_to_world,
                output_bounds, cancelled, pixel_scale=pixel_scale, **kwargs)
        job_patches.append((distort_rendering, "render_distort", original_capture_distort))
        distort_rendering.render_distort = capture_distort

    def flush_distort_input():
        if not args.capture_distort_input:
            return
        if not captured_distort_input:
            distort_capture["status"] = "native-exact-input-not-observed"
            return
        # A phase exception can bypass its normal profiler stop. Serialization
        # remains outside profiling even on that diagnostic failure path.
        profiling_hook = sys.getprofile()
        if isinstance(profiling_hook, cProfile.Profile):
            profiling_hook.disable()
        captured = captured_distort_input[0]
        image, record = captured["image"], captured["record"]
        raw = image.constBits()
        folder = output / "distort-input"
        folder.mkdir(exist_ok=True)
        started = time.perf_counter()
        record.update({"modifier": captured["modifier"].to_dict(),
            "semantic_key_repr": repr(captured["key"]),
            "scope_repr": repr(captured["scope"]), "raw_file": "distort-input/input.bin",
            "data_path": "input.bin",
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "flush_outside_timed_phases_and_profilers": True})
        key = captured["key"]
        upstream = key[1] if isinstance(key, tuple) and len(key) > 1 else None
        if isinstance(upstream, tuple) and upstream[:1] == ("stage-input",):
            record["source_key_repr"] = repr(upstream[1])
        with (folder / "input.bin").open("wb") as target:
            written = target.write(raw)
        record.update({"raw_bytes_written": written,
            "flush_ms_untimed": (time.perf_counter() - started) * 1000})
        (folder / "metadata.json").write_text(json.dumps(record, indent=2), "utf-8")
        distort_capture.update({"status": "raw-saved", "record": record})
        # Release the extra diagnostic pin after serialization; it was never
        # inserted into source/effect/retained/private or durable caches.
        captured_distort_input[0] = {"image": None, "record": record}

    if args.job_telemetry:
        jobs = canvas._effect_jobs
        def digest(value):
            return hashlib.blake2b(repr(value).encode("utf-8"), digest_size=16).hexdigest()
        def scope_details(scope):
            entity_ids = []
            numbers = []
            def inspect(value, depth=0):
                if depth > 4:
                    return
                if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value.lower()):
                    entity_ids.append(value)
                elif isinstance(value, int) and not isinstance(value, bool):
                    numbers.append(value)
                elif isinstance(value, (tuple, list)):
                    for item in value[:16]:
                        inspect(item, depth + 1)
            inspect(scope)
            role = scope[0] if isinstance(scope, tuple) and scope and isinstance(scope[0], str) else type(scope).__name__
            return {"scope_digest": digest(scope), "role": role[:64],
                    "entity_ids": entity_ids[:4], "numbers": numbers[:12]}
        def append_job_event(event):
            with telemetry_lock:
                if len(job_telemetry["events"]) < 2000:
                    job_telemetry["events"].append(event)
        def closure_metadata(compute):
            data = []
            code = getattr(compute, "__code__", None)
            names = getattr(code, "co_freevars", ())
            values = [(name, cell.cell_contents)
                      for name, cell in zip(names, getattr(compute, "__closure__", ()) or ())]
            defaults = getattr(compute, "__defaults__", ()) or ()
            argument_names = getattr(code, "co_varnames", ())[:getattr(code, "co_argcount", 0)]
            values.extend(zip(argument_names[-len(defaults):], defaults) if defaults else ())
            values.extend((getattr(compute, "__kwdefaults__", {}) or {}).items())
            for name, value in values[:32]:
                if isinstance(value, QImage):
                    data.append({"name": name, "type": "QImage", "size": [value.width(), value.height()],
                                 "bytes": value.sizeInBytes(), "cache_key": int(value.cacheKey()),
                                 "format": value.format().value})
                elif isinstance(value, (QRect, QRectF)):
                    data.append({"name": name, "type": type(value).__name__, "rect": list(value.getRect())})
                elif type(value).__module__.startswith("numpy") and hasattr(value, "shape"):
                    data.append({"name": name, "type": "array", "shape": list(value.shape),
                                 "dtype": str(value.dtype), "bytes": int(value.nbytes)})
                elif name in {"modifier", "effect"} and hasattr(value, "modifier_type"):
                    data.append({"name": name, "type": str(value.modifier_type)})
                elif type(value).__name__ == "HalftoneRegionSnapshot":
                    image = value.image
                    data.append({"name": name, "type": "HalftoneRegionSnapshot",
                                 "size": [image.width(), image.height()], "bytes": image.sizeInBytes(),
                                 "frame_size": list(value.frame_size), "origin": list(value.origin),
                                 "output": list(value.output)})
            return data
        def sample_caches():
            regions = tuple(getattr(jobs, "_regions", {}).values())
            values = {"source_bytes": canvas._modifier_source_cache_bytes,
                      "effect_bytes": canvas._modifier_render_cache_bytes,
                      "retained_bytes": jobs.retained_bytes,
                      "retained_shared_bytes": jobs.retained_shared_bytes,
                      "bytes_in_flight": jobs.bytes_in_flight,
                      "retained_entries": len(jobs.retained),
                      "assembly_entries": len(regions),
                      "assembly_partial_entries": sum(not entry.complete for entry in regions),
                      "assembly_complete_entries": sum(entry.complete for entry in regions),
                      "assembly_pixels_bytes": sum(int(entry.image.sizeInBytes()) for entry in regions),
                      "assembly_metadata_bytes": sum(entry.metadata_bytes for entry in regions),
                      "assembly_covered_tiles": sum(len(entry.covered) for entry in regions),
                      "assembly_reserved_tiles": sum(max(0, (entry.metadata_bytes - 256) // 192)
                                                     for entry in regions),
                      "retained_total_records": len(jobs.retained) + len(regions),
                      "retained_shared_entries": len(jobs._retained_shared),
                      "retained_unique_images": len(jobs._retained_images),
                      "source_entries": len(canvas._modifier_source_cache),
                      "effect_entries": len(canvas._modifier_render_cache)}
            for name, value in values.items():
                job_telemetry["peaks"][name] = max(value, job_telemetry["peaks"].get(name, 0))
            samples = job_telemetry["cache_samples"]
            if len(samples) < 2000 and (not samples or time.perf_counter() - samples[-1]["t"] > .5
                    or any(abs(value - samples[-1][name]) >
                           (8 if name.endswith("entries") or name.endswith("images") else 2 * 1024**2)
                           for name, value in values.items())):
                samples.append({"t": round(time.perf_counter(), 3), **values})
        job_telemetry["retention_limits"] = {"records": jobs.retained_limit,
                                             "shared_records": jobs.retained_limit // 2,
                                             "bytes": jobs.retained_budget,
                                             "shared_bytes": jobs.retained_budget // 2}
        job_telemetry["retention_events"] = []
        job_telemetry["large_retention_events"] = []
        large_storage_metadata = {}
        def large_retention_event(event, scope, key=None, image=None, **details):
            storage = int(image.cacheKey()) if image is not None else jobs._retained_shared.get(scope)
            known = large_storage_metadata.get(storage)
            size = int(image.sizeInBytes()) if image is not None else (
                jobs._retained_images.get(storage, (0, 0))[0] if storage is not None else 0)
            entities = scope_details(scope)
            if (size < 100 * 1024 * 1024
                    and "5fdaca6872c9485fb0ec4729e8eb7542" not in entities["entity_ids"]):
                return
            if image is not None and (storage in large_storage_metadata or len(large_storage_metadata) < 128):
                known = large_storage_metadata[storage] = {
                    "source_size": [image.width(), image.height()], "source_bytes": size,
                    "source_format": image.format().value}
            events = job_telemetry["large_retention_events"]
            if len(events) < 512:
                events.append({"event": event, "t": round(time.perf_counter(), 6),
                    "operation": payload.get("active_phase", {}).get("operation"),
                    "source_cache_key": storage, "key_digest": digest(key) if key is not None else None,
                    **entities, **(known or {"source_bytes": size}), **details,
                    "protected": scope in jobs._retained_shared,
                    "storage_aliases": jobs._retained_images.get(storage, (0, 0))[1],
                    "shared_storage_aliases": jobs._retained_shared_images.get(storage, (0, 0))[1],
                    "retained_bytes": jobs.retained_bytes, "retained_shared_bytes": jobs.retained_shared_bytes,
                    "retained_records": len(jobs.retained), "assembly_records": len(jobs._regions),
                    "protected_records": len(jobs._retained_shared)})
        original_retained_unprotect = jobs._retained_unprotect
        def tracked_retained_unprotect(scope):
            storage = jobs._retained_shared.get(scope)
            if storage is not None:
                caller = sys._getframe(1)
                context = caller.f_locals
                details = {}
                if caller.f_code.co_name == "retained_put":
                    requested_shared = bool(context.get("shared"))
                    extra = (int(context.get("size", 0)) if requested_shared
                        and context.get("storage") not in jobs._retained_shared_images else 0)
                    details = {"incoming_shared": requested_shared,
                        "incoming_size": int(context.get("size", 0)),
                        "incoming_scope": scope_details(context.get("scope")),
                        "shared_byte_pressure": jobs.retained_shared_bytes + extra >
                            context.get("shared_budget", jobs.retained_budget // 2),
                        "shared_record_pressure": len(jobs._retained_shared) + int(requested_shared) >
                            context.get("shared_limit", max(1, jobs.retained_limit // 2))}
                large_retention_event("large_unprotect", scope,
                    reason=caller.f_code.co_name, **details)
            return original_retained_unprotect(scope)
        jobs._retained_unprotect = tracked_retained_unprotect
        job_patches.append((jobs, "_retained_unprotect", original_retained_unprotect))
        original_retained_remove = jobs.retained_remove
        def tracked_retained_remove(scope, key=None):
            entry = jobs.retained.get(scope)
            if entry is not None and (key is None or entry[0] == key):
                caller = sys._getframe(1)
                reason = caller.f_code.co_name
                byte_pressure = record_pressure = False
                if reason == "_evict_retained":
                    context = caller.f_back.f_locals
                    caller_name = caller.f_back.f_code.co_name
                    extra = context.get("size", 0)
                    if caller_name == "retained_put":
                        extra = 0 if context.get("storage") in jobs._retained_images else extra
                    byte_pressure = jobs.retained_bytes + extra > context.get("budget", jobs.retained_budget)
                    record_pressure = (len(jobs.retained) + len(getattr(jobs, "_regions", {}))
                                       >= jobs.retained_limit)
                    reason = "budget-eviction/" + caller_name
                if reason == "retained_put":
                    context = caller.f_locals
                    if context.get("scope") == scope:
                        reason = "replace-existing-scope"
                    else:
                        size, storage = context.get("size", 0), context.get("storage")
                        extra = 0 if storage in jobs._retained_images else size
                        byte_pressure = jobs.retained_bytes + extra > context.get("budget", jobs.retained_budget)
                        record_pressure = (len(jobs.retained) + len(getattr(jobs, "_regions", {}))
                                           >= jobs.retained_limit)
                        reason = "budget-eviction"
                shared = scope in jobs._retained_shared
                operation = payload.get("active_phase", {}).get("operation")
                retention_counts[(operation, reason, byte_pressure, record_pressure, shared)] += 1
                large_retention_event("large_remove", scope, entry[0], entry[1], reason=reason,
                    byte_pressure=byte_pressure, record_pressure=record_pressure, shared=shared)
                events = job_telemetry["retention_events"]
                if len(events) < 256:
                    events.append({"event": "retained_remove", "operation": operation,
                        "reason": reason, "byte_pressure": byte_pressure, "record_pressure": record_pressure,
                        "shared": shared, **scope_details(scope), "key_digest": digest(entry[0]),
                        "source_cache_key": int(entry[1].cacheKey()),
                        "source_size": [entry[1].width(), entry[1].height()],
                        "records": len(jobs.retained), "shared_records": len(jobs._retained_shared),
                        "assembly_records": len(getattr(jobs, "_regions", {})),
                        "bytes": jobs.retained_bytes})
            return original_retained_remove(scope, key)
        original_retained_put = jobs.retained_put
        def tracked_retained_put(scope, key, image, state=None, **kwargs):
            storage = int(image.cacheKey())
            aliases = jobs._retained_images.get(storage, (0, 0))[1]
            previous = jobs.retained.get(scope)
            aliases -= int(previous is not None and int(previous[1].cacheKey()) == storage)
            operation = payload.get("active_phase", {}).get("operation")
            role = scope[0] if isinstance(scope, tuple) and scope else type(scope).__name__
            retention_counts[(operation, ("put-existing-pixel-alias/" if aliases else "put-new-pixels/") + str(role),
                              False, False, bool(kwargs.get("shared")))] += 1
            large_retention_event("large_put_begin", scope, key, image,
                requested_shared=bool(kwargs.get("shared")), force=bool(kwargs.get("force")),
                caller=sys._getframe(1).f_code.co_name)
            result = original_retained_put(scope, key, image, state, **kwargs)
            large_retention_event("large_put_end", scope, key, image, admitted=result,
                requested_shared=bool(kwargs.get("shared")), force=bool(kwargs.get("force")))
            if result is False:
                retention_counts[(operation, "admission-declined", False, False, bool(kwargs.get("shared")))] += 1
            sample_caches()
            return result
        if hasattr(jobs, "_region_remove"):
            original_region_remove = jobs._region_remove
            def tracked_region_remove(key):
                entry = jobs._regions.get(key)
                if entry is not None:
                    caller = sys._getframe(1)
                    reason = caller.f_code.co_name
                    byte_pressure = record_pressure = False
                    if reason == "_evict_retained":
                        context = caller.f_back.f_locals
                        caller_name = caller.f_back.f_code.co_name
                        extra = context.get("size", 0)
                        if caller_name == "retained_put":
                            extra = 0 if context.get("storage") in jobs._retained_images else extra
                        byte_pressure = jobs.retained_bytes + extra > context.get("budget", jobs.retained_budget)
                        record_pressure = len(jobs.retained) + len(jobs._regions) >= jobs.retained_limit
                        reason = "budget-eviction/" + caller_name
                    operation = payload.get("active_phase", {}).get("operation")
                    retention_counts[(operation, "assembly-" + reason,
                                      byte_pressure, record_pressure, bool(entry.complete))] += 1
                    events = job_telemetry["retention_events"]
                    if len(events) < 256:
                        events.append({"event": "assembly_remove", "operation": operation,
                            "reason": reason, "byte_pressure": byte_pressure,
                            "record_pressure": record_pressure, "complete": bool(entry.complete),
                            "key_digest": digest(key), "source_size": [entry.image.width(), entry.image.height()],
                            "covered_tiles": len(entry.covered), "reserved_tiles": max(0, (entry.metadata_bytes - 256) // 192),
                            "pixels_bytes": int(entry.image.sizeInBytes()), "metadata_bytes": entry.metadata_bytes,
                            "records": len(jobs.retained), "assembly_records": len(jobs._regions),
                            "bytes": jobs.retained_bytes})
                return original_region_remove(key)
            jobs._region_remove = tracked_region_remove
        original_request = jobs.request
        def tracked_request(scope, key, compute, size, **kwargs):
            job_telemetry["requests"] += 1
            pair = (digest(scope), digest(key))
            active = [job for job in jobs.running_jobs if not job[2].is_set()] + list(jobs.pending.values())
            same = any(job[:2] == (scope, key) for job in active)
            old = [job for job in active if job[0] == scope and job[1] != key]
            if same:
                job_telemetry["active_same_key_requests"] += 1
            if old:
                job_telemetry["scope_replacements"] += 1
                append_job_event({"event": "scope_replacement", **scope_details(scope),
                                  "operation": payload.get("active_phase", {}).get("operation"),
                                  "old_key_digests": [digest(job[1]) for job in old[:4]],
                                  "new_key_digest": pair[1], "snapshot_bytes": int(size),
                                  "require_exact": bool(kwargs.get("require_exact"))})
            if not same and completed_keys[pair]:
                job_telemetry["requests_for_completed_keys"] += 1
                if pair in repeated_keys or len(repeated_keys) < 10000:
                    repeated_keys[pair] += 1
                append_job_event({"event": "completed_key_requested_again", **scope_details(scope),
                                  "operation": payload.get("active_phase", {}).get("operation"),
                                  "key_digest": pair[1], "snapshot_bytes": int(size),
                                  "require_exact": bool(kwargs.get("require_exact"))})
            elif not same and completed_semantic_keys[pair[1]]:
                job_telemetry.setdefault("completed_semantic_keys_in_different_scope", 0)
                job_telemetry["completed_semantic_keys_in_different_scope"] += 1
                append_job_event({"event": "completed_semantic_key_requested_in_new_scope", **scope_details(scope),
                                  "operation": payload.get("active_phase", {}).get("operation"),
                                  "key_digest": pair[1], "snapshot_bytes": int(size)})
            requested_at = time.perf_counter()
            details = {**scope_details(scope), "key_digest": pair[1], "snapshot_bytes": int(size),
                       "operation": payload.get("active_phase", {}).get("operation"),
                       "require_exact": bool(kwargs.get("require_exact")),
                       "kernel": getattr(getattr(compute, "__code__", None), "co_name", type(compute).__name__),
                       "closure": closure_metadata(compute)}
            def tracked_compute(cancelled):
                started = time.perf_counter()
                worker_pair = (details["operation"], pair[0], pair[1])
                effect_type = next((entry["type"] for entry in details["closure"]
                                   if entry["name"] in ("effect", "modifier")), details["role"])
                aggregate_key = (details["operation"], details["role"], effect_type)
                with telemetry_lock:
                    if worker_pair in worker_pair_counts or len(worker_pair_counts) < 10000:
                        worker_pair_counts[worker_pair] += 1
                    else:
                        job_telemetry["dropped_key_observations"] += 1
                append_job_event({"event": "job_started", "t": round(started, 3),
                                  "queue_ms": round((started - requested_at) * 1000, 3), **details})
                try:
                    return compute(cancelled)
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    with telemetry_lock:
                        total = worker_totals.setdefault(aggregate_key, {"completed_events": 0,
                            "compute_ms": 0., "maximum_compute_ms": 0., "cancelled_events": 0})
                        total["completed_events"] += 1
                        total["compute_ms"] += elapsed
                        total["maximum_compute_ms"] = max(total["maximum_compute_ms"], elapsed)
                        total["cancelled_events"] += int(bool(cancelled()))
                    append_job_event({"event": "job_finished", "t": round(time.perf_counter(), 3),
                                      "compute_ms": round(elapsed, 3),
                                      "cancelled": bool(cancelled()), **details})
            try:
                return original_request(scope, key, tracked_compute, size, **kwargs)
            finally:
                sample_caches()
        original_cancel = jobs.cancel
        def tracked_cancel(**kwargs):
            job_telemetry["cancels"] += 1
            location = traceback.extract_stack(limit=4)[-2]
            site = (Path(location.filename).name, location.name, location.lineno)
            cancel_sites[site] += 1
            append_job_event({"event": "cancel", "callsite": list(site),
                              "clear_retained": kwargs.get("clear_retained", True),
                              "running": len(jobs.running_jobs), "pending": len(jobs.pending)})
            return original_cancel(**kwargs)
        original_poll = jobs.poll
        def tracked_poll():
            result = original_poll()
            sample_caches()
            return result
        original_ready = canvas._effect_result_ready
        def tracked_ready(scope, key):
            # The QTimer retains its originally bound poll callback. Observe
            # publication here so timer completions and explicit polls both
            # contribute, while failures/cancellations are excluded.
            if scope is not None:
                retained = jobs.retained.get(("result", scope))
                if retained is not None and retained[0] == key:
                    pair = (digest(scope), digest(key))
                    if pair in completed_keys or len(completed_keys) < 10000:
                        completed_keys[pair] += 1
                    else:
                        job_telemetry["dropped_key_observations"] += 1
                    if pair[1] in completed_semantic_keys or len(completed_semantic_keys) < 10000:
                        completed_semantic_keys[pair[1]] += 1
            sample_caches()
            return original_ready(scope, key)
        for name, function in (("request", tracked_request), ("cancel", tracked_cancel), ("poll", tracked_poll),
                               ("retained_remove", tracked_retained_remove), ("retained_put", tracked_retained_put)):
            job_patches.append((jobs, name, getattr(jobs, name)))
            setattr(jobs, name, function)
        job_patches.append((canvas, "_effect_result_ready", original_ready))
        canvas._effect_result_ready = tracked_ready
    payload = {
        "method": "Hidden native Qt canvas with production ModifierControls handlers; sources loaded without recovery or saving; edits in memory only. Native effect kernels use the ordinary backend. MainWindow inspector layout and Blender are excluded. Inclusive CPU wall timings do not establish physical pointer-to-display latency.",
        "harness_sha256": hashlib.sha256(harness_source).hexdigest(),
        "production_source_manifest_sha256": hashlib.sha256(
            json.dumps(production_manifest, sort_keys=True).encode()).hexdigest(),
        "input_source_manifest_sha256": hashlib.sha256(
            json.dumps(input_manifest, sort_keys=True).encode()).hexdigest(),
        "input_source_files": len(input_manifest),
        "transform_release_signal": True,
        "comparability": {
            "transform_release": "This harness emits interactionFinished after commit. Earlier captures before that correction omitted the signal; model and first-paint timings remain useful, while navigator lifecycle timings are not matched.",
            "transform_fixture": "This harness proves actual translation/handle gestures, changed artwork geometry and one own commit command before Undo. Earlier v13 helpers started nominal translation at the pivot and created no artwork transform; those translate observations are pivot-only and their subsequent Undo may remove an unrelated activation command. Earlier Escape-at-center checks likewise did not prove cancellation of an artwork move.",
            "selection_memory": "R5 restores normal remembered-Raster setup metadata through Canvas selection APIs before the original strict whole-model assertion; current target/tool and command identities are preserved for native verification.",
            "reparent": "Page-direct targets use a temporary child parent. Earlier captures skipped that dedicated phase, while their compound operation also reparented the target.",
            "presentation": "RasterCanvasWidget and GpuCanvasWidget captures are separate datasets; compare like widget classes and recorded DPR/context.",
        },
        "project": str(args.project.resolve()), "chapter_id": chapter.chapter_id,
        "source_root": str(args.source_root.resolve()) if args.source_root else str(ROOT),
        "chapter_name": chapter.name, "document_size": [chapter.width, chapter.height],
        "counts": {"layers": len(chapter.layers), "objects": len(chapter.objects),
                   "modifiers": dict(Counter(m.modifier_type for m in chapter.modifiers.values())),
                   "masks": len(chapter.masks)},
        "platform": args.platform, "canvas": args.canvas, "profile_enabled": args.profile,
        "first_paint_profile_enabled": args.profile_first_paint,
        "monitor_enabled": args.monitor, "navigator_enabled": args.navigator,
        "persistent_cache_binding": disk_binding_metadata,
        "planning_telemetry_enabled": args.planning_telemetry,
        "process_telemetry_enabled": args.process_telemetry,
        "live_evidence_enabled": args.live_evidence,
        "distort_input_capture": distort_capture,
        "phase_timeout_seconds": args.phase_timeout,
        "frame_capture": "When enabled, first-paint and end-of-settle QWidget grabs are saved separately. Qt capture may trigger an extra repaint; its time and projection-render delta are recorded separately from the first paint timing." if args.save_frames else "disabled",
        "transform_modes": args.transform_modes.split(","),
        "target_center_y_override": args.target_center_y,
        "parameter_mask_line": args.parameter_mask_line,
        "load_ms": round(load_ms, 3), "results": results,
    }

    planning = {"calls": {}, "total_ms": {}, "maximum_ms": {}, "largest_area": {},
                "active": {}, "recent": [], "signatures": {}}
    def planning_snapshot():
        with telemetry_lock:
            return {**planning, "preparation": {key: dict(value) for key, value in
                                               planning.get("preparation", {}).items()}}
    last_planning_checkpoint = 0.
    if args.planning_telemetry:
        from comic_editor.ui import effect_pipeline, distort_rendering, thumbnail_effects, viewport_masking
        from comic_editor.render.tile_graph import TileGraph

        def instrument_planning(owner, name, metadata):
            original = getattr(owner, name)
            def measured(*values, **kwargs):
                nonlocal last_planning_checkpoint
                details = metadata(values, kwargs)
                started = time.perf_counter()
                planning["calls"][name] = planning["calls"].get(name, 0) + 1
                signature = json.dumps(details, sort_keys=True)
                signatures = planning["signatures"].setdefault(name, {})
                if signature in signatures or len(signatures) < 512:
                    signatures[signature] = signatures.get(signature, 0) + 1
                rectangle = details.get("bounds", details.get("requested"))
                if rectangle:
                    area = max(0., rectangle[2]) * max(0., rectangle[3])
                    if area > planning["largest_area"].get(name, {}).get("area", -1):
                        planning["largest_area"][name] = {"area": area, **details}
                token = f"{threading.get_ident()}:{name}:{planning['calls'][name]}"
                planning["active"][token] = {"started": started, **details}
                if started - last_planning_checkpoint >= 2.:
                    last_planning_checkpoint = started
                    (output / "planning-telemetry.json").write_text(json.dumps(planning_snapshot(), indent=2), "utf-8")
                try:
                    return original(*values, **kwargs)
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    planning["active"].pop(token, None)
                    planning["total_ms"][name] = planning["total_ms"].get(name, 0.) + elapsed
                    planning["maximum_ms"][name] = max(elapsed, planning["maximum_ms"].get(name, 0.))
                    if elapsed > 50.:
                        planning["recent"].append({"function": name, "ms": elapsed, **details})
                        del planning["recent"][:-64]
            job_patches.append((owner, name, original))
            setattr(owner, name, measured)

        instrument_planning(effect_pipeline, "_stage_plan", lambda values, kwargs: {
            "bounds": list(values[1].getRect()),
            "required": list(values[6].getRect()) if values[6] is not None else None,
            "modifiers": [{"id": m.modifier_id, "type": m.modifier_type} for m in values[2]],
            "interactive": bool(values[0]._interactive_render)})
        def stage_metadata(values, kwargs):
            image = values[1]
            source_key = kwargs.get("source_key")
            scope = kwargs.get("request_scope")
            return {"bounds": list(values[2].getRect()),
                    "required": list(kwargs["required"].getRect()) if kwargs.get("required") is not None else None,
                    "modifiers": [{"id": m.modifier_id, "type": m.modifier_type} for m in values[3]],
                    "source_size": [image.width(), image.height()], "source_bytes": image.sizeInBytes(),
                    "source_cache_key": int(image.cacheKey()), "source_format": image.format().value,
                    "semantic_source_digest": hashlib.blake2b(repr(source_key).encode(), digest_size=16).hexdigest(),
                    "scope_digest": hashlib.blake2b(repr(scope).encode(), digest_size=16).hexdigest(),
                    "provisional": bool(kwargs.get("provisional", False)),
                    "exact": bool(getattr(values[0], "_projection_exact", False)),
                    "deferred": bool(getattr(values[0], "_projection_defer_effects", False))}
        instrument_planning(effect_pipeline, "render_stages", stage_metadata)
        instrument_planning(viewport_masking, "mask_output", lambda values, kwargs: {
            "bounds": list(values[2].getRect()), "visible": list(values[5].getRect()),
            "source_size": [values[1].width(), values[1].height()],
            "source_cache_key": int(values[1].cacheKey()), "source_format": values[1].format().value,
            "mask_id": values[4].mask_id,
            "target_id": getattr(kwargs.get("target"), "object_id", getattr(kwargs.get("target"), "layer_id", None)),
            "semantic_source_digest": hashlib.blake2b(repr(kwargs.get("source_key")).encode(), digest_size=16).hexdigest(),
            "exact": bool(getattr(values[0], "_projection_exact", False)),
            "deferred": bool(getattr(values[0], "_projection_defer_effects", False))})
        instrument_planning(distort_rendering, "distort_bounds", lambda values, kwargs: {
            "bounds": list(values[0].getRect()), "modifier": values[1].modifier_id,
            "type": values[1].modifier_type})
        instrument_planning(TileGraph, "region", lambda values, kwargs: {
            "stage": values[1], "requested": list(values[2].getRect()),
            "frame": list(values[0].nodes[values[1]].frame.getRect()),
            "dependency_node_count": len(values[0].nodes),
            "dependency_tile_span": [
                max(0, math.ceil(values[2].right() / values[0].tile_size)
                    - math.floor(values[2].left() / values[0].tile_size)),
                max(0, math.ceil(values[2].bottom() / values[0].tile_size)
                    - math.floor(values[2].top() / values[0].tile_size))]})
        # Actual live-draft admission and frame sizes distinguish a compact
        # kernel bottleneck from an unsupported/native-source fallback. Keep
        # safe metadata only; never serialize artwork or modifier parameters.
        original_capture_scale = thumbnail_effects.capture_scale
        planning["capture_scales"] = {}
        def tracked_capture_scale(owner, bounds, modifiers):
            ratio = original_capture_scale(owner, bounds, modifiers)
            details = {"operation": payload.get("active_phase", {}).get("operation"),
                       "caller": sys._getframe(1).f_code.co_name,
                       "bounds": list(bounds.getRect()), "ratio": ratio,
                       "draft_admitted": thumbnail_effects.live_effect_draft(owner),
                       "stack_supported": thumbnail_effects.compact_effects_supported(modifiers),
                       "modifiers": [{"id": m.modifier_id, "type": m.modifier_type} for m in modifiers],
                       "channel": getattr(owner, "_effect_preview_channel", "canvas"),
                       "exact": bool(getattr(owner, "_projection_exact", False)),
                       "base_alpha": bool(owner._render_base_alpha),
                       "mask_contributor": int(owner._rendering_mask_contributor)}
            signature = hashlib.blake2b(json.dumps(details, sort_keys=True).encode(), digest_size=16).hexdigest()
            rows = planning["capture_scales"]
            if signature in rows:
                rows[signature]["count"] += 1
            elif len(rows) < 1024:
                rows[signature] = {"count": 1, **details}
            return ratio
        job_patches.append((thumbnail_effects, "capture_scale", original_capture_scale))
        thumbnail_effects.capture_scale = tracked_capture_scale

        # Source preparation records inspect only detached image values and
        # the immutable preparation cache, including on worker threads. They
        # never read a canvas/model in a worker. Actual cacheKey fingerprints
        # distinguish a reused native predecessor from freshly assembled
        # images with the same semantic stage input/output geometry.
        planning["preparation"] = {}
        planning["gui_thread_id"] = threading.get_ident()
        preparation_rows = planning["preparation"]
        def preparation_record(kind, details, elapsed=0.):
            signature = json.dumps({"kind": kind, **details}, sort_keys=True)
            with telemetry_lock:
                row = preparation_rows.get(signature)
                if row is None and len(preparation_rows) < 2048:
                    row = preparation_rows[signature] = {"kind": kind, "count": 0,
                                                        "total_ms": 0., "max_ms": 0., **details}
                if row is not None:
                    row["count"] += 1
                    row["total_ms"] += elapsed
                    row["max_ms"] = max(row["max_ms"], elapsed)
        def image_metadata(image):
            return {"thread_id": threading.get_ident(), "cache_key": int(image.cacheKey()),
                    "size": [image.width(), image.height()], "format": image.format().value,
                    "bytes": image.sizeInBytes()}
        original_prepared_get = distort_rendering.PreparedDistortCache._get
        def tracked_prepared_get(owner, key):
            result = original_prepared_get(owner, key)
            if isinstance(key, tuple) and key and key[0] == "source":
                preparation_record("prepared_source_lookup", {
                    "thread_id": threading.get_ident(), "cache_id": id(owner),
                    "cache_key": key[1], "size": list(key[2:4]), "format": key[4],
                    "interpolation": key[5], "edges": key[6], "hit": result is not None})
            return result
        job_patches.append((distort_rendering.PreparedDistortCache, "_get", original_prepared_get))
        distort_rendering.PreparedDistortCache._get = tracked_prepared_get
        original_prepared_put = distort_rendering.PreparedDistortCache._put
        def tracked_prepared_put(owner, key, value, arrays):
            source = isinstance(key, tuple) and key and key[0] == "source"
            if source:
                storage_bytes = distort_rendering._array_storage_bytes(arrays)
                details = {"thread_id": threading.get_ident(), "cache_id": id(owner),
                    "cache_key": key[1], "size": list(key[2:4]), "format": key[4],
                    "interpolation": key[5], "edges": key[6], "working_bytes": storage_bytes,
                    "budget_bytes": owner.budget, "over_budget_rejection": storage_bytes > owner.budget}
            result = original_prepared_put(owner, key, value, arrays)
            if source:
                preparation_record("prepared_source_admission", details)
            return result
        job_patches.append((distort_rendering.PreparedDistortCache, "_put", original_prepared_put))
        distort_rendering.PreparedDistortCache._put = tracked_prepared_put
        original_prepared_source = distort_rendering.PreparedDistortCache.source
        def tracked_prepared_source(owner, image, interpolation="bilinear", edges="transparent"):
            details = {**image_metadata(image), "cache_id": id(owner),
                       "interpolation": interpolation, "edges": edges}
            begin = time.perf_counter()
            try:
                return original_prepared_source(owner, image, interpolation, edges)
            finally:
                preparation_record("prepared_source", details, (time.perf_counter() - begin) * 1000)
        job_patches.append((distort_rendering.PreparedDistortCache, "source", original_prepared_source))
        distort_rendering.PreparedDistortCache.source = tracked_prepared_source
        original_rgba = distort_rendering._rgba
        def tracked_rgba(image):
            details = image_metadata(image)
            begin = time.perf_counter()
            try:
                return original_rgba(image)
            finally:
                preparation_record("source_rgba_conversion", details, (time.perf_counter() - begin) * 1000)
        job_patches.append((distort_rendering, "_rgba", original_rgba))
        distort_rendering._rgba = tracked_rgba

    pending_live_images = []
    live_evidence = {"method": "Observe existing service requests; no extra decode/cache lookup. Image hashes are computed after timed first paint. Explicit current-model diagnostic captures/retries are untimed and INTERACTIVE only. Up to8 COW diagnostic images can add about32MiB at RGBA8 outside ordinary rendering budgets; causal evidence only.",
                     "renders": [], "source_decodes": [], "capture_scales": [], "render_counts": {},
                     "dropped_images": 0, "current_model_captures": []}
    if args.live_evidence:
        payload["live_evidence"] = live_evidence

    def probe_identifier():
        if args.probe_modifier:
            return args.probe_modifier
        target = canvas.chapter.objects.get(canvas.selected_object_id)
        return next((identifier for identifier in getattr(target, "modifier_ids", ())
                     if canvas.chapter.modifiers[identifier].modifier_type == "distort_twirl"), None)

    def live_state():
        modifier_id = probe_identifier()
        modifier = canvas.chapter.modifiers.get(modifier_id)
        value = (modifier.parameters.get(args.probe_parameter) if hasattr(modifier, "parameters")
                 else getattr(modifier, args.probe_parameter, None))
        preview = getattr(canvas, "_projection_interaction_preview", None)
        completed = getattr(canvas, "_projection_completed_view", None)
        result = {"modifier_id": modifier_id, "parameter": args.probe_parameter, "value": value,
            "revision": canvas._document_projection.revision,
            "completed_revision": completed[2] if completed is not None else None,
            "history": getattr(canvas, "_history_generation", 0),
            "parameter_drag_id": getattr(canvas, "_modifier_parameter_drag_id", ""),
            "parameter_drag_owner_present": getattr(canvas, "_modifier_parameter_drag_owner", None) is not None,
            "frame_pending": bool(getattr(canvas, "_projection_frame_pending", False)),
            "work_waiting": bool(getattr(canvas, "_projection_work_waiting", False)),
            "render_error": str(getattr(canvas, "_projection_render_error", None)),
            "interaction_preview": None}
        if preview is not None:
            image = preview.tile.image
            result["interaction_preview"] = {"revision": preview.revision, "history": preview.history,
                "pixel_contract": preview.pixel_contract.signature, "density": preview.density,
                "coverage": list(preview.coverage.getRect()), "image_size": [image.width(), image.height()],
                "image_sha256": hashlib.sha256(bytes(image.constBits())).hexdigest()}
        return result

    def flush_live_images():
        while pending_live_images:
            row, image = pending_live_images.pop(0)
            started = time.perf_counter()
            row["image_sha256"] = hashlib.sha256(bytes(image.constBits())).hexdigest()
            row["hash_ms_outside_first_paint"] = round((time.perf_counter() - started) * 1000, 3)

    if args.live_evidence:
        from comic_editor.render.service import RenderQuality
        from comic_editor.ui import thumbnail_effects
        original_live_render = canvas._render_service.render_region
        def observed_live_render(document, request):
            started = time.perf_counter()
            result = original_live_render(document, request)
            counter = request.quality.value + "/" + result.status.value
            live_evidence["render_counts"][counter] = live_evidence["render_counts"].get(counter, 0) + 1
            if request.quality is RenderQuality.INTERACTIVE and len(live_evidence["renders"]) < 128:
                row = {"operation": payload.get("active_phase", {}).get("operation"),
                    "started_t": round(started, 6), "finished_t": round(time.perf_counter(), 6),
                    "key": list(request.key), "quality": request.quality.value,
                    "defer_effects": request.defer_effects, "region": list(request.region),
                    "pixel_size": list(request.pixel_size), "scale": request.scale,
                    "document_revision": document.revision, "document_live_preview": document.live_preview,
                    "status": result.status.value, "error": result.error, "image_null": result.image.isNull()}
                live_evidence["renders"].append(row)
                if not result.image.isNull():
                    if len(pending_live_images) < 8:
                        pending_live_images.append((row, QImage(result.image)))
                    else:
                        live_evidence["dropped_images"] += 1
            return result
        job_patches.append((canvas._render_service, "render_region", original_live_render))
        canvas._render_service.render_region = observed_live_render
        original_live_scale = thumbnail_effects.capture_scale
        def observed_live_scale(owner, bounds, modifiers):
            ratio = original_live_scale(owner, bounds, modifiers)
            if (getattr(owner, "_bounded_effect_preview", False) or owner._interactive_render) and len(live_evidence["capture_scales"]) < 256:
                live_evidence["capture_scales"].append({
                    "operation": payload.get("active_phase", {}).get("operation"),
                    "t": round(time.perf_counter(), 6), "bounds": list(bounds.getRect()), "ratio": ratio,
                    "modifiers": [{"id": m.modifier_id, "type": m.modifier_type} for m in modifiers],
                    "bounded": bool(getattr(owner, "_bounded_effect_preview", False)),
                    "exact": bool(getattr(owner, "_projection_exact", False)),
                    "interactive": bool(owner._interactive_render),
                    "channel": getattr(owner, "_effect_preview_channel", "canvas"),
                    "base_alpha": bool(owner._render_base_alpha),
                    "mask_contributor": int(owner._rendering_mask_contributor),
                    "halftone_source": bool(getattr(owner, "_rendering_halftone_source", False)),
                    "cage_source": bool(getattr(owner, "_render_cage_source", False)),
                    "tiling_geometry_present": getattr(owner, "_tiling_capture_geometry", None) is not None})
            return ratio
        job_patches.append((thumbnail_effects, "capture_scale", original_live_scale))
        thumbnail_effects.capture_scale = observed_live_scale
        for method in ("request", "result"):
            original = getattr(canvas._effect_jobs, method)
            def observed_decode(scope, key, *values, _method=method, _original=original, **kwargs):
                result = _original(scope, key, *values, **kwargs)
                if isinstance(scope, tuple) and scope[:1] == ("source-image-decode",) and len(live_evidence["source_decodes"]) < 64:
                    row = {"event": _method, "t": round(time.perf_counter(), 6),
                        "operation": payload.get("active_phase", {}).get("operation"), "object_id": scope[-1],
                        "key_digest": hashlib.blake2b(repr(key).encode(), digest_size=16).hexdigest()}
                    if _method == "request":
                        row.update({"accepted": result, "require_exact": bool(kwargs.get("require_exact")),
                                    "snapshot_bytes": values[1] if len(values) > 1 else None})
                    else:
                        row.update({"ready": result is not None,
                                    "image_size": [result.width(), result.height()] if result is not None else None})
                    live_evidence["source_decodes"].append(row)
                return result
            job_patches.append((canvas._effect_jobs, method, original))
            setattr(canvas._effect_jobs, method, observed_decode)

    def emit(value):
        print(json.dumps(value), flush=True)

    def checkpoint():
        (output / "results.json").write_text(json.dumps(payload, indent=2), "utf-8")
        if args.planning_telemetry:
            (output / "planning-telemetry.json").write_text(json.dumps(planning_snapshot(), indent=2), "utf-8")
        if profiles:
            (output / "profiles.txt").write_text("\n\n".join(profiles), "utf-8")
        if args.monitor:
            (output / "monitor.json").write_text(json.dumps(monitor.snapshot(), indent=2), "utf-8")
        if args.job_telemetry:
            job_telemetry["retention_counts"] = [{"operation": key[0], "reason": key[1],
                "byte_pressure": key[2], "record_pressure": key[3], "shared": key[4], "count": count}
                for key, count in retention_counts.most_common()]
            job_telemetry["cancel_sites"] = [{"callsite": list(site), "count": count} for site, count in cancel_sites.most_common()]
            job_telemetry["repeated_keys"] = [{"scope_digest": key[0], "key_digest": key[1], "count": count}
                                                for key, count in repeated_keys.most_common(40)]
            with telemetry_lock:
                job_telemetry["actual_worker_pair_counts"] = [{"operation": key[0],
                    "scope_digest": key[1], "key_digest": key[2], "count": count}
                    for key, count in worker_pair_counts.items()]
                job_telemetry["worker_compute_totals"] = [{"operation": key[0], "role": key[1],
                    "effect_type": key[2], **values} for key, values in worker_totals.items()]
                (output / "job-telemetry.json").write_text(json.dumps(job_telemetry, indent=2), "utf-8")

    def stats():
        jobs = canvas._effect_jobs
        regions = tuple(getattr(jobs, "_regions", {}).values())
        result = {"projection": canvas._document_projection.snapshot(),
                "revision": canvas._document_projection.revision,
                "presented_revision": getattr(canvas, "_projection_presented_revision", -1),
                "frame_pending": getattr(canvas, "_projection_frame_pending", False),
                "render_error": str(getattr(canvas, "_projection_render_error", None)),
                "effect_pending": len(jobs.pending), "effect_running": len(jobs.running_jobs),
                "submitted": jobs.submitted, "completed": jobs.completed, "discarded": jobs.discarded,
                "source_bytes": canvas._modifier_source_cache_bytes,
                "effect_bytes": canvas._modifier_render_cache_bytes,
                "retained_bytes": jobs.retained_bytes,
                "retained_records": len(jobs.retained),
                "retained_total_records": len(jobs.retained) + len(regions),
                "assembly_partial_records": sum(not entry.complete for entry in regions),
                "assembly_complete_records": sum(entry.complete for entry in regions),
                "assembly_pixels_bytes": sum(int(entry.image.sizeInBytes()) for entry in regions),
                "assembly_metadata_bytes": sum(entry.metadata_bytes for entry in regions),
                "assembly_covered_tiles": sum(len(entry.covered) for entry in regions),
                "assembly_reserved_tiles": sum(max(0, (entry.metadata_bytes - 256) // 192)
                                               for entry in regions),
                "input": canvas.performance_snapshot()}
        if disk_binding is not None:
            backing = disk_binding.backing
            result["persistent_cache"] = {
                "entries": len(backing.entries), "hits": backing.hits, "misses": backing.misses,
                "loads": backing.loads, "reads": len(backing.reads), "ready_bytes": backing.ready_bytes,
                "saved": backing.saved, "recording": backing.recording,
                "writes": len(backing.writes), "source_hash_pending": len(disk_binding.dependencies.hashes),
                "dependency_memo_entries": len(disk_binding.dependencies.memo),
                "tile_dependency_memo_entries": len(disk_binding.dependencies.tile_memo),
                "projection_key_entries": len(disk_binding._keys),
                "observed_entries": len(disk_binding.observed),
            }
        return result

    def render():
        canvas._visual_frame_timer.stop()
        canvas._flush_visual_dirty()
        canvas.repaint()

    def process_memory():
        if not args.process_telemetry:
            return None
        started = time.perf_counter()
        if sys.platform != "win32":
            return {"available": False, "reason": "Windows process counters unavailable"}
        import ctypes
        from ctypes import wintypes
        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {"available": False, "winerror": ctypes.get_last_error()}
        sample = {"available": True, "t": round(time.perf_counter(), 6),
            "working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "private_bytes": int(counters.PrivateUsage),
            "pagefile_bytes": int(counters.PagefileUsage),
            "peak_pagefile_bytes": int(counters.PeakPagefileUsage),
            "page_fault_count": int(counters.PageFaultCount),
            "read_ms": round((time.perf_counter() - started) * 1000, 3)}
        payload.setdefault("process_memory_samples", []).append(sample)
        return sample

    def settle(limit=None):
        begin = time.perf_counter()
        progress_at = begin + 15.
        frames = 0
        stable = 0
        while time.perf_counter() - begin < (args.settle_seconds if limit is None else limit):
            canvas._effect_jobs.poll()
            render()
            flush_live_images()
            frames += 1
            app.processEvents()
            jobs = canvas._effect_jobs
            if (args.planning_telemetry or args.job_telemetry) and time.perf_counter() >= progress_at:
                payload.setdefault("active_phase", {})["settle_progress"] = {
                    "elapsed_ms": round((time.perf_counter() - begin) * 1000, 3),
                    "frames": frames, "state": stats()}
                process_memory()
                checkpoint()
                progress_at = time.perf_counter() + 15.
            ready = (not jobs.running_jobs and not jobs.pending
                     and not getattr(canvas, "_projection_frame_pending", False)
                     and (args.canvas == "gpu" or
                          not canvas._scene_dirty_full and canvas._scene_dirty_widget.isEmpty()))
            stable = stable + 1 if ready else 0
            if stable >= 2:
                return True, (time.perf_counter() - begin) * 1000, frames
            time.sleep(.003)
        return False, (time.perf_counter() - begin) * 1000, frames

    def phase(name, action=lambda: None, *, settle_after=True):
        name = phase_prefix + name
        phase_started = time.perf_counter()
        emit({"phase": "begin", "operation": name})
        payload["active_phase"] = {"operation": name, "milestone": "before action",
                                   "started_t": phase_started}
        if args.phase_timeout or args.planning_telemetry:
            checkpoint()
        before = stats()
        memory_before = process_memory()
        live_before = live_state() if args.live_evidence else None
        profiler = cProfile.Profile() if args.profile else None
        paint_profiler = cProfile.Profile() if args.profile_first_paint else None
        watchdog = None
        if args.phase_timeout > 0:
            def expired():
                # Diagnostic-only watchdog: never inspect a QWidget or live
                # document off-thread. The main thread stores safe metadata
                # before each potentially blocking call.
                with (output / "phase-timeout-threads.txt").open("w", encoding="utf-8") as stream:
                    faulthandler.dump_traceback(file=stream, all_threads=True)
                if profiler:
                    try:
                        profiler.dump_stats(str(output / (name + "-timeout.prof")))
                    except Exception:
                        pass
                if paint_profiler:
                    try:
                        paint_profiler.dump_stats(str(output / (name + "-first-paint-timeout.prof")))
                    except Exception:
                        pass
                (output / "phase-timeout.json").write_text(json.dumps({
                    "reason": "Diagnostic phase watchdog expired; process exits without saving the project",
                    "limit_seconds": args.phase_timeout, "active_phase": payload["active_phase"],
                    "completed_operations": len(results), "planning": planning_snapshot(),
                }, indent=2), "utf-8")
                os._exit(124)
            watchdog = threading.Timer(args.phase_timeout, expired)
            watchdog.daemon = True
            watchdog.start()
        if profiler:
            profiler.enable()
        started = time.perf_counter()
        action_started = started
        accepted = action()
        action_ms = (time.perf_counter() - started) * 1000
        geometry = {}
        identifier = canvas.selected_object_id
        if (args.phase_timeout or args.planning_telemetry) and identifier and identifier in canvas.chapter.objects:
            obj = canvas.chapter.objects[identifier]
            geometry = {"id": identifier, "type": obj.object_type,
                        "position": [obj.x, obj.y], "transform_frame": getattr(obj, "transform_frame", None),
                        "transform_quad": getattr(obj, "transform_quad", None),
                        "world_quad": canvas.object_world_quad(identifier),
                        "rigs": [{"id": key, "type": canvas.chapter.modifiers[key].modifier_type,
                                  "frame": canvas.chapter.modifiers[key].frame,
                                  "center": getattr(canvas.chapter.modifiers[key], "center", None),
                                  "radius": getattr(canvas.chapter.modifiers[key], "radius", None),
                                  "points": getattr(canvas.chapter.modifiers[key], "points", None),
                                  "source_points": getattr(canvas.chapter.modifiers[key], "source_points", None)}
                                 for key in obj.modifier_ids if hasattr(canvas.chapter.modifiers[key], "frame")]}
        payload["active_phase"] = {"operation": name, "milestone": "before first paint",
                                   "started_t": phase_started, "action_started_t": action_started,
                                   "action_ms": action_ms, "geometry": geometry, "state": stats()}
        if args.phase_timeout or args.planning_telemetry:
            checkpoint()
        if paint_profiler:
            paint_profiler.enable()
        started = time.perf_counter()
        paint_started = started
        try:
            render()
        finally:
            frame_ms = (time.perf_counter() - started) * 1000
            if paint_profiler:
                paint_profiler.disable()
        first = stats()
        memory_first = process_memory()
        flush_live_images()
        live_first = live_state() if args.live_evidence else None
        capture_metadata = {}
        def save_frame(label):
            prior = canvas._document_projection.snapshot()["renders"]
            begin = time.perf_counter()
            canvas.grab().save(str(output / (name + "-" + label + ".png")))
            capture_metadata[label] = {
                "ms": round((time.perf_counter() - begin) * 1000, 3),
                "projection_renders_delta": canvas._document_projection.snapshot()["renders"] - prior,
                "state": stats(),
            }
        if args.save_frames:
            save_frame("first-paint")
        if settle_after:
            settled, settle_ms, frames = settle()
        else:
            settled, settle_ms, frames = None, 0., 0
        if args.save_frames:
            save_frame("settled" if settled else "end-of-phase")
        if profiler:
            profiler.disable()
            stream = io.StringIO()
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumtime").print_stats(24)
            profiles.append(name + "\n" + stream.getvalue())
            profiler.dump_stats(str(output / (name + ".prof")))
        if paint_profiler:
            stream = io.StringIO()
            pstats.Stats(paint_profiler, stream=stream).strip_dirs().sort_stats("cumtime").print_stats(24)
            profiles.append(name + " (first paint only; action and settlement excluded)\n" + stream.getvalue())
            paint_profiler.dump_stats(str(output / (name + "-first-paint.prof")))
        if watchdog:
            watchdog.cancel()
        row = {"operation": name, "accepted": accepted if isinstance(accepted, bool) else None,
               "started_t": round(phase_started, 6), "action_started_t": round(action_started, 6),
               "paint_started_t": round(paint_started, 6),
               "first_paint_finished_t": round(paint_started + frame_ms / 1000, 6),
               "first_paint_profile_enabled": bool(paint_profiler),
               "action_ms": round(action_ms, 3), "paint_ms": round(frame_ms, 3),
               "scene_ready_ms": round(action_ms + frame_ms, 3),
               "settled": settled, "settle_ms": round(settle_ms, 3), "settle_frames": frames,
               "camera": [canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation],
               "jobs_submitted_delta": first["submitted"] - before["submitted"],
               "projection_renders_delta": first["projection"]["renders"] - before["projection"]["renders"],
               "first": first, "final": stats()}
        if args.process_telemetry:
            row["process_memory"] = {"before": memory_before, "after_first_paint": memory_first,
                                     "final": process_memory()}
        if args.live_evidence:
            row["live_state"] = {"before": live_before, "after_first_paint": live_first,
                                 "final": live_state()}
        if capture_metadata:
            row["captures"] = capture_metadata
        results.append(row)
        payload.pop("active_phase", None)
        checkpoint()
        emit(row)
        return row

    def changed(hierarchy=False):
        canvas.documentChanged.emit(QRectF())
        if hierarchy:
            canvas.hierarchyChanged.emit()
        canvas.update()

    def camera(y, scale=None):
        canvas.center_y = y
        if scale is not None:
            canvas.scale = scale
        canvas.cameraChanged.emit()
        canvas.update()

    def select(identifier):
        canvas.set_selection("object", identifier)
        quad = canvas.object_world_quad(identifier)
        if quad:
            camera(args.target_center_y if args.target_center_y is not None else sum(y for _, y in quad) / 4)
        return quad

    def model_edit(callback, label):
        before = canvas.chapter.to_dict()
        callback()
        canvas.push_model_change(before, canvas.chapter.to_dict(), label)
        changed(hierarchy=True)

    def undo():
        canvas.command_stack.undo()
        canvas._clear_transform_preview()

    def commit_transform():
        canvas._commit_object_transform()
        # Native release emits this signal immediately after the model commit.
        canvas.interactionFinished.emit()

    def translation_origin(object_id, quad):
        """Choose an interior move affordance through ordinary hit testing."""
        obj = canvas.chapter.objects[object_id]
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(*point) for point in quad]))
        path.closeSubpath()
        handles, rotate, pivot = canvas._transform_control_points(quad, canvas._transform_pivot)
        controls_points = [*handles, rotate.toTuple(), pivot.toTuple()]
        tolerance = 14 / max(canvas.scale, .05)
        for u, v in ((.25, .25), (.75, .25), (.25, .75), (.75, .75), (.35, .25), (.65, .75)):
            weights = ((1 - u) * (1 - v), u * (1 - v), u * v, (1 - u) * v)
            origin = QPointF(sum(weight * point[0] for weight, point in zip(weights, quad)),
                             sum(weight * point[1] for weight, point in zip(weights, quad)))
            if (path.contains(origin)
                    and min(math.dist(origin.toTuple(), point) for point in controls_points) > tolerance
                    and canvas._selected_object_transform_hit(obj, quad, origin) == ("translate", None)):
                return origin
        raise AssertionError("Target has no interior translation point clear of transform controls")

    def same_commands(before, after):
        return len(before) == len(after) and all(old is new for old, new in zip(before, after))

    def presentation_metadata():
        details = {"widget_class": type(canvas).__name__, "device_pixel_ratio": canvas.devicePixelRatioF(),
                   "logical_size": [canvas.width(), canvas.height()], "gpu_context_valid": False}
        context = canvas.context() if isinstance(canvas, GpuCanvasWidget) else None
        if context is not None:
            details["gpu_context_valid"] = context.isValid()
            surface = context.format()
            details["gpu_context_version"] = [surface.majorVersion(), surface.minorVersion()]
            details["gpu_context_profile"] = surface.profile().value
            if context.isValid():
                canvas.makeCurrent()
                try:
                    getter = getattr(context.functions(), "glGetString", None)
                    if getter:
                        renderer = getter(0x1F01)  # GL_RENDERER, ordinary context metadata only.
                        if isinstance(renderer, (bytes, bytearray)):
                            details["gpu_renderer"] = renderer.decode("utf-8", "replace")
                        elif isinstance(renderer, str):
                            details["gpu_renderer"] = renderer
                except Exception as error:
                    details["renderer_metadata_error"] = str(error)[:160]
                finally:
                    canvas.doneCurrent()
        return details

    def benchmark_selection_identity():
        return {"kind": canvas.selected_kind, "id": canvas.selected_id,
            "object_id": canvas.selected_object_id, "layer_id": canvas.active_layer_id,
            "page_id": canvas.active_page_id, "entities": [list(item) for item in canvas.selected_entities],
            "tool": canvas.tool.value}

    def restore_benchmark_selection_memory(original_last_rasters):
        """Undo setup's remembered-raster side effect through normal selection.

        Leave the current benchmark target/tool selected for the unchanged
        current native/cold-reference verification. No model fields are
        assigned, ignored, normalized or omitted from the strict comparison.
        """
        before_selection = benchmark_selection_identity()
        before_tool = canvas.tool
        undo_before = tuple(canvas.command_stack._undo)
        redo_before = tuple(canvas.command_stack._redo)
        revision_before = canvas.command_stack.revision
        before = {key: layer.last_raster_id for key, layer in canvas.chapter.layers.items()}
        restored_via_selection = []
        for layer_id, remembered in original_last_rasters.items():
            layer = canvas.chapter.layers.get(layer_id)
            if layer is None:
                raise AssertionError("Selection-memory restoration found a missing original layer")
            if layer.last_raster_id == remembered:
                continue
            obj = canvas.chapter.objects.get(remembered)
            if not isinstance(obj, RasterObject) or obj.parent_layer_id != layer_id:
                raise AssertionError("Original remembered Raster cannot be restored through ordinary selection")
            canvas.set_selection("object", remembered)
            if layer.last_raster_id != remembered:
                raise AssertionError("Ordinary selection did not restore the remembered Raster")
            restored_via_selection.append({"layer_id": layer_id, "object_id": remembered})
        if restored_via_selection:
            entities = [tuple(item) for item in before_selection["entities"]]
            if len(entities) > 1:
                canvas.set_selection_set(entities, (before_selection["kind"], before_selection["id"]))
            elif before_selection["kind"] and before_selection["id"]:
                canvas.set_selection(before_selection["kind"], before_selection["id"], activate_default_tool=False)
            else:
                canvas.clear_selection()
            canvas.set_tool(before_tool)
        after = {key: layer.last_raster_id for key, layer in canvas.chapter.layers.items()}
        after_selection = benchmark_selection_identity()
        commands_preserved = (len(undo_before) == len(canvas.command_stack._undo)
            and all(old is new for old, new in zip(undo_before, canvas.command_stack._undo))
            and len(redo_before) == len(canvas.command_stack._redo)
            and all(old is new for old, new in zip(redo_before, canvas.command_stack._redo))
            and canvas.command_stack.revision == revision_before)
        record = {"method": "Ordinary Canvas.set_selection restores each prior remembered Raster, followed by an activate_default_tool=False selection/tool round trip for existing target/native oracle provenance. No direct field writes or ignored model fields.",
            "original_last_rasters": original_last_rasters, "before_restore_last_rasters": before,
            "after_restore_last_rasters": after, "restored_via_selection": restored_via_selection,
            "before_restore_selection": before_selection, "after_restore_selection": after_selection,
            "remembered_rasters_restored": after == original_last_rasters,
            "selection_and_tool_roundtrip_preserved": after_selection == before_selection,
            "command_identities_and_revision_preserved": commands_preserved,
            "whole_model_assertion_follows": True}
        if not all(record[key] for key in ("remembered_rasters_restored",
                "selection_and_tool_roundtrip_preserved", "command_identities_and_revision_preserved")):
            raise AssertionError("Benchmark selection metadata/selection/history restoration failed")
        return record


    try:
        checkpoint()
        emit({"phase": "loaded", **{key: value for key, value in payload.items() if key != "results"}})
        phase("initial", window.show)
        payload["presentation"] = presentation_metadata()
        # This saved raster is visible on the first page and has substantial
        # ordinary source data. Prefer it, then fall back to a valid drawing.
        preferred = args.object_id or "49a55205999a431da9e5c95cb06c8bba"
        default_id = preferred if preferred in chapter.objects else next(
            key for key, obj in chapter.objects.items() if isinstance(obj, (RasterObject, ImageObject)))
        target_ids = args.objects.split(",") if args.objects else [default_id]
        if any(identifier not in chapter.objects for identifier in target_ids):
            raise ValueError("A requested benchmark object does not exist")
        def snapshot_cold_reference(batch, check_label):
            """Pin current input buffers and native output without sharing caches."""
            if not args.verify_cold_reference or payload.get("cold_reference_snapshot"):
                return
            if "parameter-mask" in groups and check_label != "painted-parameter-mask":
                return
            import base64
            from dataclasses import asdict
            folder = output / "cold-reference"
            folder.mkdir(exist_ok=True)
            entries = [(phase_id, tile) for phase_id, items in batch for tile in items]
            target = canvas.chapter.objects.get(canvas.selected_object_id)
            preferred_phase = "top" if target and target.show_on_top else "base"
            preferred = [entry for entry in entries if entry[0] in (None, preferred_phase)] or entries
            if args.parameter_mask_line:
                x1, y1, x2, y2 = map(float, args.parameter_mask_line.split(","))
                anchors = [(x1, y1), (x2, y2), (canvas.center_x, canvas.center_y)]
            else:
                quad = canvas.object_world_quad(canvas.selected_object_id)
                anchors = (list(quad[:2]) if quad else []) + [(canvas.center_x, canvas.center_y)]
            selected, seen = [], set()
            for x, y in anchors:
                for phase_id, tile in sorted(preferred, key=lambda entry:
                        (entry[1].world_rect.center().x() - x)**2 +
                        (entry[1].world_rect.center().y() - y)**2):
                    identity = (phase_id, tuple(tile.world_rect.getRect()))
                    if identity not in seen:
                        seen.add(identity)
                        selected.append((phase_id, tile))
                        break
            patches = []
            blocks = set()
            target_quad = (canvas.object_world_quad(canvas.selected_object_id) or ()) if target else ()
            target_bounds = (QRectF(min(point[0] for point in target_quad), min(point[1] for point in target_quad),
                                   max(point[0] for point in target_quad) - min(point[0] for point in target_quad),
                                   max(point[1] for point in target_quad) - min(point[1] for point in target_quad))
                             if target_quad else QRectF())
            target_effect_bounds = (canvas._render_bounds.entity_bounds("object", canvas.selected_object_id)
                                    if target else None)
            for n, (phase_id, tile) in enumerate(selected[:3]):
                image = tile.image
                raw = bytes(image.constBits())
                filename = f"expected-{n}.bin"
                (folder / filename).write_bytes(raw)
                address = (0, math.floor(tile.world_rect.left() / 256),
                           math.floor(tile.world_rect.top() / 256))
                blocks.add((phase_id, address[1] // 4, address[2] // 4))
                # Diagnostic only: quantify visible patch content without
                # changing the raw native-format bytes used by the oracle.
                rgba = image.convertToFormat(QImage.Format_RGBA8888)
                rgba_bytes = bytes(rgba.constBits())
                pixel_counts = Counter(rgba_bytes[offset:offset + 4]
                    for row in range(rgba.height())
                    for offset in range(row * rgba.bytesPerLine(),
                                        row * rgba.bytesPerLine() + rgba.width() * 4, 4))
                opaque_white = pixel_counts.get(b"\xff\xff\xff\xff", 0)
                transparent = sum(count for pixel, count in pixel_counts.items() if pixel[3] == 0)
                pixels = image.width() * image.height()
                patches.append({"phase": phase_id, "address": address,
                    "world_rect": list(tile.world_rect.getRect()), "size": [image.width(), image.height()],
                    "format": image.format().value, "bytes_per_line": image.bytesPerLine(),
                    "sha256": hashlib.sha256(raw).hexdigest(), "file": filename,
                    "content": {"pixels": pixels, "transparent_pixels": transparent,
                        "opaque_white_pixels": opaque_white,
                        "visible_nonwhite_pixels": pixels - opaque_white - transparent,
                        "distinct_rgba8_colors": len(pixel_counts),
                        "diagnostic_conversion": "RGBA8 summary only; oracle retains original format/stride/bytes"},
                    "target_quad_bounds_intersection": list(tile.world_rect.intersected(target_bounds).getRect()),
                    "target_effect_bounds_intersection": (list(tile.world_rect.intersected(target_effect_bounds).getRect())
                                                          if target_effect_bounds is not None else None),
                    "anchors_inside_patch": [list(anchor) for anchor in anchors
                                              if tile.world_rect.contains(QPointF(*anchor))]})
            requests = []
            for phase_id, tile in entries:
                address = (0, math.floor(tile.world_rect.left() / 256),
                           math.floor(tile.world_rect.top() / 256))
                if (phase_id, address[1] // 4, address[2] // 4) in blocks:
                    requests.append({"phase": phase_id, "address": address})
            owners, buffers = {}, []
            for identifier, owner in canvas.tiles._tiles.items():
                owners[identifier] = [list(address) for address in owner]
                for address in owner:
                    if owner.backing(address) is not None and (identifier, *address) not in canvas.tiles.dirty:
                        continue
                    image = owner[address]
                    filename = f"input-{len(buffers)}.bin"
                    (folder / filename).write_bytes(bytes(image.constBits()))
                    buffers.append({"owner": identifier, "address": list(address),
                        "size": [image.width(), image.height()], "stride": image.bytesPerLine(),
                        "format": image.format().value, "device_pixel_ratio": image.devicePixelRatio(),
                        "color_profile": base64.b64encode(bytes(image.colorSpace().iccProfile())).decode("ascii"),
                        "file": filename})
            specification = {"source_root": str(production_root), "source_chapter": str(source),
                "check_label": check_label, "model": canvas.chapter.to_dict(),
                "tile_owners": owners, "tile_buffers": buffers, "patches": patches, "requests": requests,
                "settings": asdict(settings), "selected_kind": canvas.selected_kind,
                "selected_id": canvas.selected_id, "solo_entities": list(canvas._solo_entities),
                "camera": [canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation],
                "viewport": [canvas.width(), canvas.height()],
                "target_id": canvas.selected_object_id, "target_source_world_quad": list(target_quad),
                "target_source_world_quad_bounds": list(target_bounds.getRect()), "anchors": anchors,
                "target_conservative_effect_bounds": (list(target_effect_bounds.getRect())
                                                      if target_effect_bounds is not None else None),
                "configuration_tail": canvas._projection_configuration()[3:], "platform": args.platform,
                "pixel_contract": canvas.chapter.pixel_contract.signature,
                "method": "Separate-process cold synchronous scene/effect/retained/private caches; current model and exact current native input buffers; original fixed4x4 document captures and nondeferred stage demand"}
            specification_path = folder / "input.json"
            specification_path.write_text(json.dumps(specification, indent=2), "utf-8")
            payload["cold_reference_snapshot"] = {"path": str(specification_path), "check_label": check_label,
                "patches": [{key: value for key, value in patch.items() if key != "file"} for patch in patches],
                "blocks": len(blocks), "requests": len(requests), "modified_input_buffers": len(buffers)}
            checkpoint()

        def _verify_exact(check_label):
            settled, verification_settle_ms, verification_frames = settle(args.verify_seconds)
            verification_state = stats()
            complete = getattr(canvas, "_projection_completed_view", None)
            current_complete = (complete is not None
                                and complete[2] == canvas._document_projection.revision
                                and complete[0] == canvas._projection_configuration())
            if not settled or not current_complete:
                return {"verified": False, "reason": "No current-revision complete settled tile batch",
                        "settle_ms": round(verification_settle_ms, 3),
                        "settle_frames": verification_frames, "settled_state": verification_state}
            else:
                import numpy as np
                def pixel_map(batch):
                    captured = {}
                    for _, items in batch:
                        for tile in items:
                            image = tile.image
                            metadata = [list(tile.world_rect.getRect()), image.format().value,
                                        image.width(), image.height(), image.bytesPerLine()]
                            captured[str(tile.key)] = (metadata, bytes(image.constBits()))
                    return captured
                expected = pixel_map(complete[1])
                snapshot_cold_reference(complete[1], check_label)
                # Stable native-pixel fingerprints also allow a source-root
                # before/after comparison without confusing revision-bearing
                # projection keys with pixel coordinates or losing precision
                # through an 8-bit screenshot encoder.
                native_hashes = []
                for phase_id, items in complete[1]:
                    for tile in items:
                        image = tile.image
                        native_hashes.append({"phase": phase_id,
                            "world_rect": list(tile.world_rect.getRect()),
                            "format": image.format().value,
                            "size": [image.width(), image.height()],
                            "bytes_per_line": image.bytesPerLine(),
                            "sha256": hashlib.sha256(bytes(image.constBits())).hexdigest()})
                native_hashes.sort(key=lambda row: (str(row["phase"]), row["world_rect"]))
                previous = canvas._projection_async_enabled
                canvas._projection_async_enabled = False
                try:
                    recapture_started = time.perf_counter()
                    canvas._invalidate_scene_cache()
                    render()
                    recapture_ms = (time.perf_counter() - recapture_started) * 1000
                    recaptured = canvas._projection_completed_view
                    recapture_current = (recaptured is not None
                                         and recaptured[2] == canvas._document_projection.revision
                                         and recaptured[0] == canvas._projection_configuration())
                    actual = pixel_map(recaptured[1]) if recapture_current else {}
                finally:
                    canvas._projection_async_enabled = previous
                keys_equal = expected.keys() == actual.keys()
                changed_bytes, maximum = 0, 0
                if keys_equal:
                    for key in expected:
                        a, b = expected[key][1], actual[key][1]
                        if expected[key][0] != actual[key][0] or len(a) != len(b):
                            keys_equal = False
                            break
                        difference = np.abs(np.frombuffer(a, np.uint8).astype(np.int16)
                                            - np.frombuffer(b, np.uint8).astype(np.int16))
                        changed_bytes += int(np.count_nonzero(difference))
                        maximum = max(maximum, int(difference.max(initial=0)))
                return {
                    "verified": recapture_current and keys_equal and maximum == 0,
                    "synchronous_recapture_current": recapture_current, "tile_keys_match": keys_equal,
                    "tiles": len(expected), "changed_bytes": changed_bytes,
                    "maximum_byte_difference": maximum,
                    "settle_ms": round(verification_settle_ms, 3),
                    "settle_frames": verification_frames,
                    "settled_state": verification_state,
                    "synchronous_recapture_state": stats(),
                    "synchronous_recapture_ms": round(recapture_ms, 3),
                    "native_tile_hashes": native_hashes,
                    "method": "Settled asynchronous tile bytes versus forced synchronous exact recapture through the ordinary scene pipeline; completed effect caches retained, so this is publication consistency rather than an independent cold-kernel proof"}
        def verify_exact(check_label="restored-saved"):
            previous = payload.get("active_phase")
            payload["active_phase"] = previous or {
                "operation": phase_prefix + check_label + "-exact-verification", "state": stats()}
            if args.planning_telemetry or args.job_telemetry:
                checkpoint()
            try:
                return _verify_exact(check_label)
            finally:
                if previous is None:
                    payload.pop("active_phase", None)
                else:
                    payload["active_phase"] = previous
        def effective_modifiers(object_id):
            obj = canvas.chapter.objects[object_id]
            ids = list(obj.modifier_ids)
            parent_id = obj.parent_layer_id
            while parent_id:
                parent = canvas.chapter.layers[parent_id]
                ids.extend(parent.modifier_ids)
                parent_id = parent.parent_id
            return list(dict.fromkeys(ids))

        def parameter_probe(object_id):
            from PySide6.QtWidgets import QDoubleSpinBox, QSlider
            from comic_editor.render.service import RenderQuality, RenderRequest
            from comic_editor.ui.distort_controls import DistortControls
            identifier = args.probe_modifier or next((key for key in effective_modifiers(object_id)
                if canvas.chapter.modifiers[key].modifier_type == "distort_twirl"), None)
            if identifier is None:
                raise AssertionError("Parameter probe needs an actual Twirl or explicit modifier")
            widget = next((item for item in controls.findChildren(DistortControls)
                           if item.modifier.modifier_id == identifier), None)
            if widget is None:
                raise AssertionError("Selected target's production Distort controls are absent")
            slider = widget.findChild(QSlider, "distortSlider_" + args.probe_parameter)
            spin = widget.findChild(QDoubleSpinBox, "distortValue_" + args.probe_parameter)
            if slider is None or spin is None:
                raise AssertionError("Requested production parameter slider is absent")
            original = canvas.chapter.modifiers[identifier].parameters[args.probe_parameter]
            factor = 10 ** spin.decimals()
            modifier = canvas.chapter.modifiers[identifier]
            frame = getattr(modifier, "frame", None)
            center = getattr(modifier, "center", None)
            if center is None and frame is not None:
                center = [frame[0] + max(frame[2], 1e-6) / 2.,
                          frame[1] + max(frame[3], 1e-6) / 2.]
            radius = getattr(modifier, "radius", 0.)
            if not radius and frame is not None:
                radius = min(max(frame[2], 1e-6), max(frame[3], 1e-6)) / 2.
            radius = max(float(radius), 1e-6) if radius else None
            support = (QRectF(center[0] - radius, center[1] - radius, radius * 2, radius * 2)
                       if center is not None and radius is not None else None)
            view = canvas.visible_document_rect()
            probe = {"object_id": object_id, "modifier_id": identifier, "parameter": args.probe_parameter,
                     "original": original, "requested": args.probe_value,
                     "spatial_scope": {"stored_frame_world": frame, "center_world": center,
                         "radius_world": radius, "view_world": list(view.getRect()),
                         "support_bounds_world": list(support.getRect()) if support is not None else None,
                         "support_view_intersection": list(support.intersected(view).getRect())
                             if support is not None else None,
                         "method": "Stored Twirl world-space rig/defaults; angle influence is zero outside its local radius before downstream effects. Lens/Pinch may relocate affected content, so zero visible or patch difference alone does not establish stale rendering."},
                     "method": "Production DistortParameterSlider/number signal path with explicit mouse-press-equivalent begin; real slider release and history undo; MainWindow layout/hardware input excluded"}
            payload.setdefault("parameter_probes", []).append(probe)

            def current_live_capture(label):
                geometry = canvas._interaction_projection_geometry()
                if geometry is None:
                    return None
                visible, density, size = geometry
                document = canvas._render_document_state()
                canvas._render_service.configure((*document.configuration, None), document=document.identity)
                document = canvas._render_document_state()
                request = RenderRequest(tuple(visible.getRect()), density, size,
                    ('diagnostic-current-live', label), document.revision, quality=RenderQuality.INTERACTIVE)
                started = time.perf_counter()
                attempts = []
                updates_enabled = canvas.updatesEnabled()
                canvas.setUpdatesEnabled(False)
                try:
                    while True:
                        document = canvas._render_document_state()
                        request = RenderRequest(tuple(visible.getRect()), density, size,
                            ('diagnostic-current-live', label), document.revision, quality=RenderQuality.INTERACTIVE)
                        result = canvas._render_service.render_region(document, request)
                        current = (result.document == document and result.request == request
                                   and canvas._render_service.current(document, request))
                        presentable = (current and not result.image.isNull()
                                       and result.status.value in {"exact", "provisional"})
                        attempts.append({"revision": document.revision, "status": result.status.value,
                                         "result_revision": result.document.revision,
                                         "request_revision": result.request.revision,
                                         "current": bool(current), "presentable": bool(presentable),
                                         "image_null": result.image.isNull(), "error": result.error})
                        flush_live_images()
                        if (presentable or time.perf_counter() - started >= args.probe_wait_seconds):
                            break
                        # Only retry this bounded current-model request. Do not
                        # render exact presentation as a diagnostic fallback.
                        canvas._effect_jobs.poll()
                        app.processEvents()
                        time.sleep(.02)
                finally:
                    canvas.setUpdatesEnabled(updates_enabled)
                row = {"label": label, "quality": request.quality.value, "defer_effects": request.defer_effects,
                       "revision": document.revision, "status": result.status.value,
                       "world_coverage": list(request.region), "density": request.scale,
                       "pixel_size": list(request.pixel_size),
                       "result_revision": result.document.revision,
                       "request_revision": result.request.revision,
                       "current": bool(current), "presentable": bool(presentable),
                       "error": result.error, "image_null": result.image.isNull(),
                       "capture_ms_untimed": round((time.perf_counter() - started) * 1000, 3),
                       "attempts": attempts[-32:], "attempt_count": len(attempts),
                       "state": live_state()}
                live_evidence["current_model_captures"].append(row)
                flush_live_images()
                if not presentable:
                    return None
                image = result.image
                raw = bytes(image.constBits())
                row.update({"metadata": [image.width(), image.height(), image.bytesPerLine(), image.format().value],
                            "image_sha256": hashlib.sha256(raw).hexdigest()})
                filename = phase_prefix + "parameter-probe-" + label + "-current-live.png"
                image.save(str(output / filename))
                row["file"] = filename
                return row["metadata"], raw, tuple(request.region), request.scale, image.depth() // 8

            def begin_probe():
                controls.begin_parameter_drag(identifier)
                slider.setSliderDown(True)
            phase("parameter-probe-begin", begin_probe, settle_after=False)
            for n in range(args.frames):
                value = original + (args.probe_value - original) * (n + 1) / args.frames
                phase(f"parameter-probe-drag-{n}", lambda value=value:
                      slider.setValue(round(value * factor)), settle_after=False)
            phase("parameter-probe-release", lambda: slider.setSliderDown(False), settle_after=False)
            if controls._parameter_before is not None:
                raise AssertionError("Production slider release did not end its parameter history gesture")
            probe["after_release"] = live_state() if args.live_evidence else {
                "value": canvas.chapter.modifiers[identifier].parameters[args.probe_parameter]}
            if args.live_evidence:
                payload["active_phase"] = {"operation": phase_prefix + "parameter-probe-feedback"}
                observed, elapsed, frames = settle(args.probe_wait_seconds)
                probe["feedback_observation"] = {"settled": observed, "elapsed_ms": elapsed,
                                                 "frames": frames, "state": live_state()}
                newer = current_live_capture("new-angle")
                payload.pop("active_phase", None)
            phase("parameter-probe-undo", undo)
            if canvas.chapter.modifiers[identifier].parameters[args.probe_parameter] != original:
                raise AssertionError("Parameter probe undo did not restore its original angle")
            if args.live_evidence:
                older = current_live_capture("old-angle-after-undo")
                comparison = {"available": newer is not None and older is not None,
                              "method": "Untimed current-model INTERACTIVE captures after release and after actual history undo; no exact fallback or native cache publication"}
                if comparison["available"]:
                    comparison["metadata_equal"] = newer[0] == older[0]
                    comparison["world_coverage_equal"] = newer[2] == older[2]
                    comparison["density_equal"] = newer[3] == older[3]
                    if comparison["metadata_equal"]:
                        import numpy as np
                        a, b = np.frombuffer(newer[1], np.uint8), np.frombuffer(older[1], np.uint8)
                        comparison.update({"byte_equal": newer[1] == older[1],
                            "changed_bytes": int(np.count_nonzero(a != b)),
                            "maximum_byte_difference": int(np.max(np.abs(a.astype(np.int16)-b.astype(np.int16))))})
                        if (comparison["world_coverage_equal"] and comparison["density_equal"]
                                and newer[4] == older[4] and support is not None):
                            width, height, stride, _format = newer[0]
                            coverage, density, pixel_bytes = QRectF(*newer[2]), newer[3], newer[4]
                            aa, bb = a.reshape(height, stride), b.reshape(height, stride)
                            patches = []
                            for label, rectangle in (
                                ("twirl-support-bounds", support),
                                ("twirl-center-128-world", QRectF(center[0] - 64., center[1] - 64., 128., 128.))):
                                rectangle = rectangle.intersected(coverage)
                                row = {"label": label, "requested_world": list(rectangle.getRect()),
                                    "method": "Raw current INTERACTIVE image bytes at the same world patch; downstream effects can relocate Twirl influence"}
                                if rectangle.isEmpty():
                                    row["visible"] = False
                                else:
                                    x0 = max(0, math.floor((rectangle.left() - coverage.left()) * density))
                                    y0 = max(0, math.floor((rectangle.top() - coverage.top()) * density))
                                    x1 = min(width, math.ceil((rectangle.right() - coverage.left()) * density))
                                    y1 = min(height, math.ceil((rectangle.bottom() - coverage.top()) * density))
                                    before_patch = bb[y0:y1, x0*pixel_bytes:x1*pixel_bytes]
                                    after_patch = aa[y0:y1, x0*pixel_bytes:x1*pixel_bytes]
                                    row.update({"visible": bool(before_patch.size), "pixel_bounds": [x0, y0, x1, y1],
                                        "sampled_world": [coverage.left() + x0 / density, coverage.top() + y0 / density,
                                            (x1 - x0) / density, (y1 - y0) / density],
                                        "compared_bytes": int(before_patch.size),
                                        "byte_equal": bool(np.array_equal(before_patch, after_patch)),
                                        "changed_bytes": int(np.count_nonzero(before_patch != after_patch)),
                                        "maximum_byte_difference": int(np.max(np.abs(before_patch.astype(np.int16)
                                            - after_patch.astype(np.int16)))) if before_patch.size else None,
                                        "old_sha256": hashlib.sha256(before_patch.tobytes()).hexdigest(),
                                        "new_sha256": hashlib.sha256(after_patch.tobytes()).hexdigest()})
                                patches.append(row)
                            comparison["patches"] = patches
                probe["current_model_live_comparison"] = comparison
                checkpoint()

        def exercise_target(object_id):
            before_model = canvas.chapter.to_dict()
            selection_metadata_before_setup = benchmark_selection_identity()
            original_last_rasters = {key: layer.last_raster_id for key, layer in canvas.chapter.layers.items()}
            phase("target-ready", lambda: select(object_id))
            ids = effective_modifiers(object_id)
            payload.setdefault("targets", {})[object_id] = {
                "name": canvas.chapter.objects[object_id].name,
                "selection_metadata_before_setup": selection_metadata_before_setup,
                "effective_modifiers": [{"id": key, "type": canvas.chapter.modifiers[key].modifier_type,
                                         "muted": canvas.chapter.modifiers[key].muted} for key in ids]}
            if "parameter-probe" in groups:
                parameter_probe(object_id)
            if "solo" in groups:
                for n in range(args.frames):
                    phase(f"solo-{n}", lambda n=n: canvas.set_solo_entities(
                        {("object", object_id)} if n % 2 == 0 else set()))
                phase("solo-clear", lambda: canvas.set_solo_entities(set()))
            if "modifier" in groups:
                for n, identifier in enumerate(ids[:args.frames]):
                    original = canvas.chapter.modifiers[identifier].muted
                    phase(f"modifier-{n}-mute", lambda identifier=identifier, original=original:
                          controls.set_parameter(identifier, "muted", not original, True))
                    phase(f"modifier-{n}-undo", undo)
                # Exercise the actual inspector lifecycle. The existing stacks
                # remain enabled while a parameter changes across input frames.
                active = next((key for key in ids if not canvas.chapter.modifiers[key].muted), None)
                if active:
                    modifier = canvas.chapter.modifiers[active]
                    attribute = next((name for name in ("hue", "thickness", "strength", "intensity")
                                      if hasattr(modifier, name)), None)
                    if attribute:
                        original = getattr(modifier, attribute)
                        phase("parameter-begin", lambda: controls.begin_parameter_drag(active), settle_after=False)
                        for n in range(args.frames):
                            phase(f"parameter-drag-{n}", lambda n=n: controls.set_parameter(
                                active, attribute, original - (n + 1) if original >= 99 else original + (n + 1),
                                False), settle_after=False)
                        phase("parameter-commit", controls.finish_parameter_drag)
                        phase("parameter-undo", undo)
            if "transform" in groups:
                for mode in args.transform_modes.split(","):
                    if mode not in {"translate", "scale", "warp"}:
                        raise ValueError("Unknown transform mode")
                    phase(f"{mode}-ready", lambda: select(object_id))
                    canvas.set_tool(ToolKind.TRANSFORM)
                    settings.transform_mode = "free" if mode == "warp" else "uniform"
                    quad = canvas.object_world_quad(object_id)
                    origin = translation_origin(object_id, quad) if mode == "translate" else QPointF(*quad[2])
                    transform_before = canvas.chapter.to_dict()
                    world_quad_before = list(quad)
                    undo_before = tuple(canvas.command_stack._undo)
                    revision_before = canvas.command_stack.revision
                    expected_kind = "translate" if mode == "translate" else "handle"
                    check = {"origin_world": list(origin.toTuple()), "expected_drag_kind": expected_kind,
                             "world_quad_before": world_quad_before}
                    payload["targets"][object_id].setdefault("transform_checks", {})[mode] = check
                    accepted = phase(f"{mode}-begin", lambda: canvas._begin_selected_raster_transform(origin), settle_after=False)
                    check["actual_drag_kind"] = canvas._transform_drag_mode
                    if not accepted["accepted"] or canvas._transform_drag_mode != expected_kind:
                        raise AssertionError(f"{mode} did not begin its intended production gesture")
                    for n in range(args.frames):
                        phase(f"{mode}-drag-{n}", lambda n=n: canvas._update_transform_preview(
                            origin + QPointF(3 * (n + 1), 2 * (n + 1))), settle_after=False)
                    check["preview_geometry_changed"] = canvas._transform_preview_quad != canvas._transform_start_quad
                    if not check["preview_geometry_changed"]:
                        raise AssertionError(f"{mode} drag did not change preview artwork geometry")
                    phase(f"{mode}-commit", commit_transform)
                    committed_command = canvas.command_stack.top_undo_command
                    check.update({"model_changed_on_commit": canvas.chapter.to_dict() != transform_before,
                        "world_artwork_geometry_changed": canvas.object_world_quad(object_id) != world_quad_before,
                        "own_command_recorded": len(canvas.command_stack._undo) == len(undo_before) + 1
                            and canvas.command_stack.revision == revision_before + 1
                            and same_commands(undo_before, canvas.command_stack._undo[:-1])
                            and committed_command is not None
                            and committed_command.label.startswith("Transform ")})
                    if not all(check[key] for key in ("model_changed_on_commit",
                            "world_artwork_geometry_changed", "own_command_recorded")):
                        raise AssertionError(f"{mode} commit did not change artwork and create exactly one own command")
                    phase(f"{mode}-undo", undo)
                    check.update({"model_restored_after_undo": canvas.chapter.to_dict() == transform_before,
                        "world_artwork_geometry_restored": canvas.object_world_quad(object_id) == world_quad_before,
                        "prior_undo_commands_preserved": same_commands(undo_before, canvas.command_stack._undo),
                        "own_command_undone": canvas.command_stack.revision == revision_before + 2
                            and len(canvas.command_stack._redo) == 1
                            and canvas.command_stack._redo[-1] is committed_command})
                    if not all(check[key] for key in ("model_restored_after_undo", "world_artwork_geometry_restored",
                            "prior_undo_commands_preserved", "own_command_undone")):
                        raise AssertionError(f"{mode} Undo did not restore its artwork and preserve prior history")
                    canvas._clear_transform_preview()
            if "hierarchy" in groups:
                obj = canvas.chapter.objects[object_id]
                parent = canvas.chapter.layers[obj.parent_layer_id]
                target = parent.parent_id
                if target:
                    phase("reparent-to-ancestor", lambda: model_edit(
                        lambda: canvas.chapter.move_entity("object", object_id, target, 0), "Reparent benchmark"))
                    phase("reparent-undo", undo)
                else:
                    # Objects directly under a page have no existing ancestor
                    # destination. Exercise an actual parent→child move by
                    # adding a bounded child at the current object's position.
                    def reparent_to_child():
                        inverse, valid = canvas.layer_world_transform(parent.layer_id).inverted()
                        if not valid:
                            raise ValueError("Singular benchmark parent")
                        points = [inverse.map(QPointF(x, y)) for x, y in canvas.object_world_quad(object_id)]
                        left, top = min(p.x() for p in points), min(p.y() for p in points)
                        right, bottom = max(p.x() for p in points), max(p.y() for p in points)
                        layer = canvas.chapter.add_layer(parent.layer_id, "Benchmark child parent",
                            BoundGeometry.rectangle(left, top, right - left, bottom - top), index=0)
                        canvas.chapter.move_entity("object", object_id, layer.layer_id, 0)
                    phase("reparent-to-child", lambda: model_edit(reparent_to_child, "Reparent benchmark"))
                    phase("reparent-undo", undo)
            if "mask" in groups:
                def add_mask():
                    obj = canvas.chapter.objects[object_id]
                    mask = ToneMask(contributors=[])
                    canvas.chapter.masks[mask.mask_id] = mask
                    # Half opacity keeps the expensive stack in the composite.
                    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, .5, 1.)
                phase("mask-attach", lambda: model_edit(add_mask, "Attach mask benchmark"))
                phase("mask-undo", undo)
            if "parameter-mask" in groups:
                # Reproduce the native inspector's HSL intensity mask, then
                # use the production mask-stroke lifecycle and TilePatch
                # history. Direct canvas handlers are labelled in the method;
                # this does not claim hardware pointer-to-display latency.
                modifier_id = next((key for key in ids
                    if canvas.chapter.modifiers[key].modifier_type == "hsl"
                    and not canvas.chapter.modifiers[key].muted),
                    next((key for key in ids if not canvas.chapter.modifiers[key].muted), None))
                if modifier_id is None:
                    raise AssertionError("Parameter-mask target has no active modifier")
                mask_ids = []
                def add_parameter_mask():
                    mask = ToneMask(contributors=[])
                    canvas.chapter.masks[mask.mask_id] = mask
                    canvas.chapter.modifiers[modifier_id].parameter_masks["intensity"] = ParameterMaskBinding(
                        mask.mask_id, 0., 100.)
                    mask_ids.append(mask.mask_id)
                phase("parameter-mask-attach", lambda: model_edit(add_parameter_mask, "Attach parameter mask benchmark"))
                mask_id = mask_ids[0]
                phase("parameter-mask-edit-mode", lambda: canvas.set_tone_mask_mode(mask_id))
                canvas.set_tool(ToolKind.RASTER_PENCIL)
                if args.parameter_mask_line:
                    line = tuple(float(value) for value in args.parameter_mask_line.split(","))
                    if len(line) != 4:
                        raise ValueError("Parameter mask line must contain x1,y1,x2,y2")
                else:
                    quad = canvas.object_world_quad(object_id)
                    cx, cy = sum(x for x, _ in quad) / 4, sum(y for _, y in quad) / 4
                    line = (cx - 140., cy - 37., cx + 140., cy + 37.)
                start, end = QPointF(*line[:2]), QPointF(*line[2:])
                payload["targets"][object_id]["parameter_mask_case"] = {
                    "modifier": modifier_id, "mask": mask_id, "endpoints": [0., 100.],
                    "line": list(line), "brush_size": settings.pencil_size(),
                    "method": "Production set_tone_mask_mode/begin/continue/flush/end mask stroke handlers; production TilePatchCommand undo"}
                phase("parameter-mask-paint-begin", lambda: canvas._begin_mask_stroke(start, 1.), settle_after=False)
                for n in range(args.frames):
                    point = start + (end - start) * ((n + 1) / args.frames)
                    def paint_sample(point=point):
                        canvas._continue_mask_stroke(point, 1.)
                        canvas._flush_mask_samples()
                    phase(f"parameter-mask-paint-drag-{n}", paint_sample, settle_after=False)
                phase("parameter-mask-paint-commit", canvas._end_mask_stroke)
                if args.probe_after_mask:
                    parameter_probe(object_id)
                if args.verify_exact:
                    payload["active_phase"] = {"operation": phase_prefix + "parameter-mask-exact-verification",
                                               "state": stats()}
                    checkpoint()
                    payload.setdefault("parameter_mask_exact_verifications", {})[object_id] = verify_exact("painted-parameter-mask")
                    payload.pop("active_phase", None)
                    checkpoint()
                phase("parameter-mask-exit-mode", lambda: canvas.set_tone_mask_mode(""))
                phase("parameter-mask-paint-undo", undo)
                if canvas.tiles.content_bounds(mask_id) is not None:
                    raise AssertionError("Parameter mask paint undo retained painted tiles")
                phase("parameter-mask-binding-undo", undo)
            if "shape" in groups:
                compound_ids = []
                def add_compound():
                    obj = canvas.chapter.objects[object_id]
                    parent = canvas.chapter.layers[obj.parent_layer_id]
                    inverse, valid = canvas.layer_world_transform(parent.layer_id).inverted()
                    if not valid:
                        raise ValueError("Singular benchmark parent")
                    points = [inverse.map(QPointF(x, y)) for x, y in canvas.object_world_quad(object_id)]
                    left, top = min(p.x() for p in points), min(p.y() for p in points)
                    right, bottom = max(p.x() for p in points), max(p.y() for p in points)
                    layer = canvas.chapter.add_layer(parent.layer_id, "Benchmark compound",
                        BoundGeometry.rectangle(left, top, right - left, bottom - top), index=0)
                    layer.compound_enabled = True
                    child = canvas.chapter.add_layer(layer.layer_id, "Subtract",
                        BoundGeometry.circle((left + right) / 2, (top + bottom) / 2,
                                             min(right - left, bottom - top) / 8))
                    child.compound_operation = "subtract"
                    canvas.chapter.move_entity("object", object_id, layer.layer_id, 0)
                    compound_ids.extend([layer.layer_id, child.layer_id])
                phase("compound-create-around-target", lambda: model_edit(add_compound, "Compound benchmark"))
                def move_shape():
                    child = canvas.chapter.layers[compound_ids[-1]]
                    child.translate_x += 5.
                    child.translate_y += 3.
                phase("compound-shape-move", lambda: model_edit(move_shape, "Compound shape move benchmark"))
                phase("compound-shape-undo", undo)
                phase("compound-undo", undo)
            if "warp" in groups:
                candidate = next((key for key in ids
                                  if canvas.chapter.modifiers[key].modifier_type in {"cage_transform", "distort_mesh_warp"}
                                  and getattr(canvas.chapter.modifiers[key], "points", None)), None)
                if candidate:
                    def point_edit():
                        modifier = canvas.chapter.modifiers[candidate]
                        x, y = modifier.points[len(modifier.points) // 2]
                        modifier.points[len(modifier.points) // 2] = (x + 3., y + 2.)
                    phase("warp-control-edit", lambda: model_edit(point_edit, "Warp point benchmark"))
                    phase("warp-control-undo", undo)
            if "history" in groups:
                history_checks = {}
                phase("history-ready", lambda: select(object_id))
                original = canvas.chapter.to_dict()
                identifier = next((key for key in ids if not canvas.chapter.modifiers[key].muted), None)
                if identifier is not None:
                    modifier = canvas.chapter.modifiers[identifier]
                    intensity = modifier.intensity
                    depth = len(canvas.command_stack._undo)
                    phase("history-modifier-edit", lambda: controls.set_parameter(
                        identifier, "intensity", intensity - 1 if intensity >= 99 else intensity + 1, True))
                    edited = canvas.chapter.to_dict()
                    if edited == original or len(canvas.command_stack._undo) != depth + 1:
                        raise AssertionError("History edit did not create one meaningful command")
                    phase("history-modifier-undo", canvas.command_stack.undo)
                    if canvas.chapter.to_dict() != original or len(canvas.command_stack._undo) != depth:
                        raise AssertionError("History undo did not restore the model and undo position")
                    phase("history-modifier-redo", canvas.command_stack.redo)
                    if canvas.chapter.to_dict() != edited or len(canvas.command_stack._undo) != depth + 1:
                        raise AssertionError("History redo did not restore the edited model and undo position")
                    phase("history-modifier-final-undo", canvas.command_stack.undo)
                    if canvas.chapter.to_dict() != original or len(canvas.command_stack._undo) != depth:
                        raise AssertionError("Final history undo did not restore the model and undo position")
                    history_checks["modifier_undo_redo_undo"] = True
                phase("history-cancel-ready", lambda: select(object_id))
                canvas.set_tool(ToolKind.TRANSFORM)
                settings.transform_mode = "uniform"
                quad = canvas.object_world_quad(object_id)
                origin = translation_origin(object_id, quad)
                canceled_model = canvas.chapter.to_dict()
                stack_position = (len(canvas.command_stack._undo), len(canvas.command_stack._redo),
                                  canvas.command_stack.revision)
                finished = []
                def observe_finished():
                    finished.append(True)
                canvas.interactionFinished.connect(observe_finished)
                try:
                    row = phase("history-cancel-begin", lambda: canvas._begin_selected_raster_transform(origin),
                                settle_after=False)
                    if not row["accepted"] or canvas._transform_drag_mode != "translate":
                        raise AssertionError("History cancellation did not begin a production translation gesture")
                    phase("history-cancel-update", lambda: canvas._update_transform_preview(
                        origin + QPointF(7., 5.)), settle_after=False)
                    preview_geometry_changed = canvas._transform_preview_quad != canvas._transform_start_quad
                    if not preview_geometry_changed:
                        raise AssertionError("History cancellation did not change preview artwork geometry")
                    phase("history-cancel-escape", lambda: QApplication.sendEvent(canvas,
                        QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)))
                finally:
                    canvas.interactionFinished.disconnect(observe_finished)
                unchanged_position = stack_position == (
                    len(canvas.command_stack._undo), len(canvas.command_stack._redo), canvas.command_stack.revision)
                restored_cancel = canceled_model == canvas.chapter.to_dict()
                preview_cleared = canvas._transform_preview_quad is None and canvas._model_before is None
                history_checks["escape_cancel"] = {
                    "model_restored": restored_cancel, "command_stack_position_unchanged": unchanged_position,
                    "preview_cleared": preview_cleared, "interaction_finished_signals": len(finished),
                    "actual_drag_kind": "translate", "preview_geometry_changed": preview_geometry_changed,
                    "method": "Actual Canvas key event handler through QApplication.sendEvent; no manual preview clear/model restore/signal injection"}
                if not (unchanged_position and restored_cancel and preview_cleared):
                    raise AssertionError("Escape cancellation did not restore model/preview/history position")
                if args.navigator:
                    def navigator_recovered():
                        preview = window.preview
                        begin = time.perf_counter()
                        while time.perf_counter() - begin < args.navigator_wait_seconds:
                            app.processEvents()
                            canvas._effect_jobs.poll()
                            if (not preview._interaction_active() and not preview._dirty_full
                                    and not preview._dirty_bands and preview._pending_image.isNull()
                                    and preview._cache_chapter is canvas.chapter and not preview._cache.isNull()):
                                return True
                            time.sleep(.005)
                        return False
                    navigator_row = phase("history-navigator-recovery", navigator_recovered)
                    history_checks["escape_cancel"]["navigator_recovered"] = navigator_row["accepted"]
                    history_checks["escape_cancel"]["navigator_wait_ms"] = navigator_row["action_ms"]
                    if not navigator_row["accepted"]:
                        raise AssertionError("Navigator remained dirty/pending after production Escape cancellation")
                payload["targets"][object_id]["history_checks"] = history_checks
            payload["targets"][object_id]["selection_metadata_restoration"] = restore_benchmark_selection_memory(original_last_rasters)
            restored = before_model == canvas.chapter.to_dict()
            payload["targets"][object_id]["model_restored"] = restored
            if not restored:
                checkpoint()
                raise AssertionError("An in-memory benchmark edit was not fully restored")
            if args.verify_exact and not args.activate_stack:
                payload.setdefault("exact_verifications", {})[object_id] = verify_exact()
                if len(target_ids) == 1:
                    payload["exact_verification"] = payload["exact_verifications"][object_id]
            checkpoint()

        if "navigation" in groups:
            for value in args.positions.split(","):
                y = float(value)
                phase(f"nav-{y:g}-cold", lambda y=y: camera(y))
                phase(f"nav-{y:g}-warm")
                if args.verify_navigation_exact:
                    check = verify_exact(f"navigation-{y:g}")
                    if args.save_frames:
                        renders_before = canvas._document_projection.snapshot()["renders"]
                        started = time.perf_counter()
                        filename = f"nav-{y:g}-exact-{'verified' if check['verified'] else 'unverified'}.png"
                        canvas.grab().save(str(output / filename))
                        check["frame_capture"] = {"file": filename,
                            "ms": (time.perf_counter() - started) * 1000,
                            "projection_renders_delta": canvas._document_projection.snapshot()["renders"] - renders_before,
                            "method": "Diagnostic QWidget grab after verification; may trigger another paint"}
                    payload.setdefault("navigation_exact_verifications", {})[f"{y:g}"] = check
                    checkpoint()
            phase("nav-return-top", lambda: camera(300.))
            for scale in (.5, 1., 1.5, args.scale):
                phase(f"zoom-{scale:g}", lambda scale=scale: camera(300., scale))

        if "selection" in groups:
            candidates = [key for key, candidate in chapter.objects.items()
                          if isinstance(candidate, (TextObject, ImageObject, RasterObject))][:args.frames]
            for n, identifier in enumerate(candidates):
                phase(f"selection-{n}", lambda identifier=identifier: canvas.set_selection("object", identifier))

        for object_id in target_ids:
            phase_prefix = object_id[:8] + "-" if len(target_ids) > 1 else ""
            saved_model = canvas.chapter.to_dict()
            if args.activate_stack:
                phase("activate-ready", lambda: select(object_id))
                def activate_stack():
                    for key in effective_modifiers(object_id):
                        canvas.chapter.modifiers[key].muted = False
                phase("activate-entire-stack", lambda: model_edit(activate_stack, "Activate stress stack"))
                activation_changed = saved_model != canvas.chapter.to_dict()
            exercise_target(object_id)
            if args.activate_stack:
                if args.verify_active_exact:
                    payload.setdefault("active_exact_verifications", {})[object_id] = verify_exact("restored-all-active")
                    checkpoint()
                if activation_changed:
                    phase("activate-stack-undo", undo)
                restored = saved_model == canvas.chapter.to_dict()
                payload["targets"][object_id]["saved_model_restored"] = restored
                if not restored:
                    raise AssertionError("Stress activation did not restore the saved model")
                if args.verify_exact:
                    payload.setdefault("exact_verifications", {})[object_id] = verify_exact()
                    if len(target_ids) == 1:
                        payload["exact_verification"] = payload["exact_verifications"][object_id]
                checkpoint()
        payload["summary"] = {
            "operations": len(results), "maximum_scene_ready_ms": max(r["scene_ready_ms"] for r in results),
            "median_scene_ready_ms": statistics.median(r["scene_ready_ms"] for r in results),
            "unsettled": [r["operation"] for r in results if r["settled"] is False]}
        exact_checks = {f"{kind}/{identifier}": value.get("verified", False)
                        for kind in ("exact_verifications", "active_exact_verifications",
                                     "parameter_mask_exact_verifications", "navigation_exact_verifications")
                        for identifier, value in payload.get(kind, {}).items()}
        payload["validation"] = {
            "models_restored": all(value.get("model_restored", False)
                                   and value.get("saved_model_restored", True)
                                   for value in payload.get("targets", {}).values()),
            "exact_checks": exact_checks,
            "all_requested_exact_checks_verified": all(exact_checks.values()) if args.verify_exact else None,
            "pending_or_failed_exact_checks": [key for key, verified in exact_checks.items() if not verified],
            "process_exit_semantics": "Zero exit means the helper completed without an exception; exact acceptance additionally requires each requested exact check verified, source preservation and clean native wrapper return",
        }
        if args.verify_cold_reference:
            import subprocess
            snapshot = payload.get("cold_reference_snapshot")
            if snapshot is None:
                payload["cold_reference"] = {"verified": False, "reason": "No settled current-revision native batch could be snapshotted"}
            else:
                canvas.setUpdatesEnabled(False)
                canvas._visual_frame_timer.stop()
                canvas._effect_jobs.timer.stop()
                emit({"phase": "cold-reference-begin", "snapshot": snapshot, "deadline_seconds": 120})
                checkpoint()
                folder = Path(snapshot["path"]).parent
                started = time.perf_counter()
                with (folder / "stdout.log").open("w", encoding="utf-8") as stdout, \
                     (folder / "stderr.log").open("w", encoding="utf-8") as stderr:
                    child = subprocess.Popen([sys.executable, str(output / "benchmark-harness.py"),
                        "--cold-reference-input", snapshot["path"]], cwd=production_root,
                        stdout=stdout, stderr=stderr)
                    timed_out = False
                    try:
                        child.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                result_path = folder / "result.json"
                reference = json.loads(result_path.read_text("utf-8")) if result_path.exists() else {}
                reference.update({"native_returncode": child.returncode, "timed_out": timed_out,
                    "elapsed_seconds": time.perf_counter() - started,
                    "verified": bool(reference.get("verified") and child.returncode == 0 and not timed_out)})
                payload["cold_reference"] = reference
                checkpoint()
                emit({"phase": "cold-reference-end", "result": reference})
            payload["validation"]["independent_cold_reference_verified"] = payload["cold_reference"]["verified"]
            payload["validation"]["all_publication_exact_checks_verified"] = payload["validation"]["all_requested_exact_checks_verified"]
            payload["validation"]["all_requested_exact_checks_verified"] = bool(
                payload["validation"]["all_publication_exact_checks_verified"]
                and payload["cold_reference"]["verified"])
    finally:
        monitor.stop()
        payload["monitor_log"] = str(monitor.last_log_path) if monitor.last_log_path else None
        checkpoint()
        for owner, name, original in reversed(job_patches):
            setattr(owner, name, original)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        flush_distort_input()
        if disk_binding is not None:
            disk_binding.backing._assert_read_only()
            disk_binding_metadata["final_state"] = stats()["persistent_cache"]
            canvas._document_projection.backing_lookup = canvas._document_projection.backing_retain = None
            tiles.render_fingerprint = images.render_fingerprint = None
            canvas._persistent_render_cache = canvas._render_dependencies = None
            canvas._disk_cache_controller = None
            disk_binding.backing.close()
            cache_index_after = hashlib.sha256(cache_index.read_bytes()).hexdigest() if cache_index.is_file() else None
            disk_binding_metadata["index_sha256_after"] = cache_index_after
            disk_binding_metadata["index_unchanged"] = cache_index_after == cache_index_before
            cache_contents_after = cache_content_manifest()
            disk_binding_metadata["contents_unchanged"] = cache_contents_after == cache_contents_before
            disk_binding_metadata["content_manifest_sha256_after"] = hashlib.sha256(
                json.dumps(cache_contents_after, sort_keys=True).encode()).hexdigest()
            if cache_index_after != cache_index_before:
                raise AssertionError("The read-only cache index changed during profiling")
            if cache_contents_after != cache_contents_before:
                raise AssertionError("The read-only cache files changed during profiling")
        # This embedded host never enters QApplication.exec()/aboutToQuit.
        # Retire the graphics owner while the application and surface still
        # exist, rather than leaving native cleanup to interpreter teardown.
        from comic_editor.ui import point_lut
        worker = getattr(point_lut, "_worker", None)
        if worker is not None:
            worker.close()
            atexit.unregister(worker.close)
        renderer = getattr(point_lut, "_gpu", None)
        if renderer is not None:
            renderer.close()
        for attribute in ("_gpu_pattern_renderer", "_gpu_cage_renderer"):
            renderer = getattr(canvas, attribute, None)
            if renderer:
                renderer.close()
        window.close()
        window.deleteLater()
        app.processEvents()
        # processEvents() alone does not deliver all deferred widget deletion
        # events for a host that never entered exec(). Complete one ordinary
        # quit cycle so native GL widget teardown precedes application teardown.
        QTimer.singleShot(0, app.quit)
        payload["cleanup"] = {"explicit_graphics_owner_shutdown": True,
                              "deferred_widget_quit_cycle": True,
                              "event_loop_return": app.exec()}
        final_manifest = source_manifest()
        payload["source_files_changed_during_run"] = [key for key in
            sorted(production_manifest.keys() | final_manifest.keys())
            if production_manifest.get(key) != final_manifest.get(key)]
        final_input_paths = [source / "chapter.json"]
        for folder in ("raster", "masks", "images"):
            final_input_paths.extend(path for path in (source / folder).rglob("*") if path.is_file())
        final_input_manifest = {str(path.relative_to(source)).replace("\\", "/"):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(final_input_paths)}
        payload["input_source_files_changed_during_run"] = [key for key in
            sorted(input_manifest.keys() | final_input_manifest.keys())
            if input_manifest.get(key) != final_input_manifest.get(key)]
        payload["input_source_manifest_sha256_after"] = hashlib.sha256(
            json.dumps(final_input_manifest, sort_keys=True).encode()).hexdigest()
        payload.setdefault("validation", {})["source_preservation"] = {
            "production_source_unchanged": not payload["source_files_changed_during_run"],
            "native_inputs_unchanged": not payload["input_source_files_changed_during_run"],
            "persistent_cache_unchanged": (bool(disk_binding_metadata.get("index_unchanged")
                and disk_binding_metadata.get("contents_unchanged")) if disk_binding is not None else None)}
        checkpoint()
    emit({"phase": "finished", "summary": payload.get("summary"), "output": str(output)})


def cold_reference_main(specification_path):
    """A bounded child uses fresh derived caches and canonical native kernels."""
    import base64
    specification_path = Path(specification_path).resolve()
    folder = specification_path.parent
    spec = json.loads(specification_path.read_text("utf-8"))
    sys.path.insert(0, spec["source_root"])
    os.environ["QT_QPA_PLATFORM"] = spec["platform"]
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QColorSpace, QImage, QSurfaceFormat
    from PySide6.QtWidgets import QApplication, QWidget
    from comic_editor.core.images import ImageStore
    from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core.tiles import TileStore
    from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
    from comic_editor.render.service import TileBatchPolicy
    from comic_editor.ui.canvas import RasterCanvasWidget
    faulthandler.enable()
    surface = QSurfaceFormat()
    surface.setVersion(3, 3)
    surface.setProfile(QSurfaceFormat.CoreProfile)
    QSurfaceFormat.setDefaultFormat(surface)
    app = QApplication([])
    window = QWidget()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.setAttribute(Qt.WA_ShowWithoutActivating, True)
    settings = EditorSettings(**spec["settings"])
    canvas = RasterCanvasWidget(settings, window)
    canvas.setUpdatesEnabled(False)
    chapter = ChapterDocument.from_dict(spec["model"])
    tiles, images = TileStore(), ImageStore()
    source = Path(spec["source_chapter"])
    tiles.load_directory(source / "raster", {key for key, obj in chapter.objects.items() if isinstance(obj, RasterObject)})
    tiles.load_directory(source / "masks", set(chapter.masks), clear=False)
    for identifier, owner in tuple(tiles._tiles.items()):
        allowed = {tuple(address) for address in spec["tile_owners"].get(identifier, ())}
        for address in tuple(owner):
            if address not in allowed:
                del owner[address]
    for record in spec["tile_buffers"]:
        raw = (folder / record["file"]).read_bytes()
        image = QImage(raw, *record["size"], record["stride"], QImage.Format(record["format"])).copy()
        image.setDevicePixelRatio(record["device_pixel_ratio"])
        if record["color_profile"]:
            image.setColorSpace(QColorSpace.fromIccProfile(base64.b64decode(record["color_profile"])))
        tiles._object_tiles(record["owner"])[tuple(record["address"])] = image
    images.load_directory(source / "images", {key: (obj.source_filename, obj.source_mime_type)
        for key, obj in chapter.objects.items() if isinstance(obj, ImageObject)})
    canvas.resize(*spec["viewport"])
    canvas.set_document(chapter, tiles, images)
    canvas.set_selection(spec["selected_kind"], spec["selected_id"])
    canvas._solo_entities = {tuple(item) for item in spec["solo_entities"]}
    canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = spec["camera"]
    canvas._projection_async_enabled = False
    canvas._projection_defer_effects = False
    canvas._effect_jobs.timer.stop()
    canvas._visual_frame_timer.stop()
    result = {"verified": False, "check_label": spec["check_label"], "method": spec["method"],
        "presentation_widget": type(canvas).__name__, "gpu_effect_setting": settings.canvas_renderer,
        "pixel_contract": chapter.pixel_contract.signature, "patches": [],
        "initial_derived_cache_entries": {"scene": canvas._document_projection.snapshot()["tiles"],
            "effects": len(canvas._modifier_render_cache), "sources": len(canvas._modifier_source_cache),
            "retained": len(canvas._effect_jobs.retained), "assemblies": len(canvas._effect_jobs._regions)}}
    def checkpoint():
        (folder / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
    checkpoint()
    try:
        if json.dumps(canvas._projection_configuration()[3:]) != json.dumps(spec["configuration_tail"]):
            raise AssertionError("Cold reference scene configuration differs from the saved exact batch")
        if any(result["initial_derived_cache_entries"].values()):
            raise AssertionError("Cold reference unexpectedly inherited derived cache entries")
        phases = list(dict.fromkeys(row["phase"] for row in spec["requests"]))
        for phase in phases:
            requests = [ProjectionRequest(ProjectionAddress(*row["address"]))
                        for row in spec["requests"] if row["phase"] == phase]
            configuration = canvas._projection_configuration()
            canvas._render_service.configure((*configuration, phase), document=configuration[:3])
            document = canvas._render_document_state()
            statuses = []
            def capture(request):
                captured = canvas._render_service.render_region(document, request)
                statuses.append(captured.status.value)
                return captured
            started = time.perf_counter()
            batch = canvas._render_service.render_tiles(document, requests,
                TileBatchPolicy((canvas.center_x, canvas.center_y)), phase=phase, defer_effects=False,
                capture=capture)
            result.setdefault("captures", []).append({"phase": phase, "requests": len(requests),
                "ms": (time.perf_counter() - started) * 1000, "error": batch.error,
                "pending": batch.pending, "fixed_blocks": batch.blocks_started, "statuses": statuses})
            for patch in spec["patches"]:
                if patch["phase"] != phase:
                    continue
                image, exact = batch.tiles[ProjectionAddress(*patch["address"])]
                expected = (folder / patch["file"]).read_bytes()
                actual = bytes(image.constBits()) if exact and not image.isNull() else b""
                metadata_equal = (exact and [image.width(), image.height()] == patch["size"]
                    and image.bytesPerLine() == patch["bytes_per_line"] and image.format().value == patch["format"])
                result["patches"].append({"phase": phase, "world_rect": patch["world_rect"],
                    "address": patch["address"], "exact": bool(exact), "metadata_equal": bool(metadata_equal),
                    "byte_equal": bool(metadata_equal and actual == expected),
                    "expected_sha256": patch["sha256"], "actual_sha256": hashlib.sha256(actual).hexdigest(),
                    "changed_bytes": sum(a != b for a, b in zip(actual, expected)) if len(actual) == len(expected) else None})
            checkpoint()
        result["verified"] = (len(result["patches"]) == len(spec["patches"])
                              and bool(result["patches"]) and all(row["byte_equal"] for row in result["patches"]))
        result["queued_jobs"] = canvas._effect_jobs.submitted
        if result["queued_jobs"]:
            raise AssertionError("Cold reference enqueued an asynchronous kernel")
    finally:
        checkpoint()
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        from comic_editor.ui import point_lut
        worker = getattr(point_lut, "_worker", None)
        if worker is not None:
            worker.close()
            atexit.unregister(worker.close)
        for renderer in (getattr(point_lut, "_gpu", None),
                getattr(canvas, "_gpu_pattern_renderer", None), getattr(canvas, "_gpu_texture_renderer", None)):
            if renderer is not None:
                renderer.close()
        window.close()
        window.deleteLater()
        QTimer.singleShot(0, app.quit)
        result["event_loop_return"] = app.exec()
        checkpoint()
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--cold-reference-input":
        cold_reference_main(sys.argv[2])
    else:
        main()
