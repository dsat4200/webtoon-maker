"""Whole saved chapter rendering benchmark, with no project saves or Blender.

Run manually, not via pytest. All artwork comes from an explicitly isolated
copy. Private canvas helpers measure synchronous scene-ready work, not physical
input-to-display latency. JSON states whether each first frame was provisional
and compares it with the settled frame at the identical camera/model state.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts" / "render-architecture-20260926"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--label", default="current")
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--positions", default="300,6456,12000,27000,42000")
parser.add_argument("--settle-seconds", type=float, default=45.)
parser.add_argument("--skip-edits", action="store_true")
parser.add_argument("--save-all", action="store_true")
parser.add_argument("--native-frame", action="store_true",
                    help="Exercise real paintEvent in a window that never appears or activates.")
parser.add_argument("--dirty-checks", action="store_true",
                    help="Check unchanged pixels after local and cross-block dirty redraws.")
args = parser.parse_args()
OUT = (ARTIFACTS / args.label).resolve()
assert OUT.is_relative_to(ARTIFACTS.resolve()) and OUT != ARTIFACTS.resolve()
OUT.mkdir(parents=True, exist_ok=True)
COPY = (ARTIFACTS / "project-copy").resolve()
assert COPY.is_relative_to((ROOT / ".artifacts").resolve()) and COPY.name == "project-copy"
assert (COPY / "series.json").is_file()
sys.path.insert(0, str(ARTIFACTS / "baseline-source" if args.baseline else ROOT))
os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["QT_TLS_BACKEND"] = "schannel"

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFontDatabase, QImage, QOpenGLContext
from PySide6.QtWidgets import QApplication
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core import settings as settings_module
from comic_editor.ui.canvas import create_canvas, ToolKind
from comic_editor.ui import effect_pipeline, interactive_effects, interactive_strokes
from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer

settings_module.settings_path = lambda: OUT / "isolated-settings.json"
app = QApplication.instance() or QApplication([])
for font in ("segoeui.ttf", "arial.ttf", "times.ttf", "comic.ttf"):
    filename = Path("C:/Windows/Fonts") / font
    if filename.is_file():
        QFontDatabase.addApplicationFont(str(filename))


def emit(value):
    print(json.dumps(value), flush=True)


def manifest():
    return {str(path.relative_to(COPY)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(COPY.rglob("*")) if path.is_file()}


before_hashes = manifest()
(OUT / "project-files-before.json").write_text(json.dumps(before_hashes, indent=2))
chapter, tiles, images = SeriesRepository(COPY).load_chapter(
    "0a72f08009294aa0a3d14e6a38e22bbb", include_images=True)
settings = EditorSettings(canvas_renderer="auto", snap_to_grid=False,
                          predictive_ink=False, grid_overlay_visible=False)
settings.pencil_size_px[settings.active_pencil_size] = 8
canvas = create_canvas(settings)
canvas.resize(1000, 800)
if args.native_frame:
    # Context creation is intentionally outside document cold-frame timing.
    # Neither flag allows this benchmark window to appear or take focus.
    canvas.setAttribute(Qt.WA_DontShowOnScreen, True)
    canvas.setAttribute(Qt.WA_ShowWithoutActivating, True)
    canvas.show()
    app.processEvents()
    assert canvas.isValid(), "Native frame measurement requires a valid OpenGL canvas"
canvas.set_document(chapter, tiles, images)
canvas.center_x, canvas.center_y, canvas.scale = 540., 300., 1.
events = []
originals = []


def wrap(module, name, kind):
    if not hasattr(module, name):
        return
    original = getattr(module, name)
    originals.append((module, name, original))

    def tracked(*values, **kwargs):
        image = next((v for v in values if isinstance(v, QImage)), None)
        modifier_names = []
        for value in values:
            if hasattr(value, "modifier_type"):
                modifier_names.append(value.modifier_type)
            elif isinstance(value, (list, tuple)):
                modifier_names.extend(v.modifier_type for v in value if hasattr(v, "modifier_type"))
        event = {"kind": kind, "modifiers": modifier_names,
                 "size": [image.width(), image.height()] if image is not None else None}
        events.append(event)
        start = time.perf_counter()
        try:
            return original(*values, **kwargs)
        finally:
            event["ms"] = round((time.perf_counter() - start) * 1000, 3)
    setattr(module, name, tracked)


for module in (effect_pipeline, interactive_effects):
    wrap(module, "apply_modifier_stack", "cpu_stack")
wrap(interactive_effects, "_draft", "draft")
wrap(effect_pipeline, "_pattern_draft", "pattern_draft")
wrap(interactive_strokes, "_draft", "stroke_draft")
wrap(GpuPatternRenderer, "render", "gpu_pattern_readback")
wrap(canvas, "_modifier_source_cache_put", "source_capture")


def event_summary(items):
    return {"counts": dict(Counter(item["kind"] for item in items)),
            "cpu_ms": round(sum(item.get("ms", 0) for item in items if item["kind"] == "cpu_stack"), 3),
            "gpu_ms": round(sum(item.get("ms", 0) for item in items if item["kind"] == "gpu_pattern_readback"), 3),
            "largest_image_pixels": max((item["size"][0]*item["size"][1] for item in items if item["size"]), default=0)}


def cache_stats():
    jobs = canvas._effect_jobs
    result = {"ordinary_output_bytes": canvas._modifier_render_cache_bytes,
            "ordinary_source_bytes": canvas._modifier_source_cache_bytes,
            "retained_exact_bytes": jobs.retained_bytes,
            "jobs_submitted": jobs.submitted, "jobs_completed": jobs.completed,
            "jobs_discarded": jobs.discarded, "jobs_pending": len(jobs.pending),
            "running_scope": str(jobs.running[0]) if jobs.running else None}
    projection = getattr(canvas, "_document_projection", None)
    if projection is not None:
        result["projection"] = projection.snapshot()
    presentation = getattr(canvas, "_document_presentation_stats", None)
    if presentation is not None:
        from dataclasses import asdict
        result["presentation"] = asdict(presentation)
    return result


def render():
    canvas._visual_frame_timer.stop()
    canvas._flush_visual_dirty()
    if args.native_frame:
        # grabFramebuffer() invokes paintGL again, which bypasses the editor's
        # paintEvent and can return black. Read the already-painted FBO instead.
        # This deliberate validation readback is timed separately and excluded
        # from scene_ready_ms; it is not work performed by the editor itself.
        import ctypes
        started = time.perf_counter()
        canvas.repaint()
        paint_ms = (time.perf_counter()-started)*1000
        started = time.perf_counter()
        canvas.makeCurrent()
        width = round(canvas.width()*canvas.devicePixelRatioF())
        height = round(canvas.height()*canvas.devicePixelRatioF())
        array = np.zeros((height, width, 4), np.uint8)
        gl = ctypes.WinDLL("opengl32")
        gl.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        canvas.context().functions().glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
        gl.glReadPixels(0, 0, width, height, 0x1908, 0x1401, array.ctypes.data)
        canvas.doneCurrent()
        array = np.ascontiguousarray(array[::-1])
        result = QImage(array.data, width, height, width*4, QImage.Format_RGBA8888_Premultiplied).copy()
        result.setDevicePixelRatio(canvas.devicePixelRatioF())
        render.native_paint_ms = paint_ms
        render.validation_readback_ms = (time.perf_counter()-started)*1000
        return result
    canvas._ensure_scene_cache()
    return QImage(canvas._scene_cache)


def pixel_difference(first, exact):
    a = np.frombuffer(first.constBits(), np.uint8).astype(np.int16)
    b = np.frombuffer(exact.constBits(), np.uint8).astype(np.int16)
    delta = np.abs(a - b)
    return {"changed_bytes": int(np.count_nonzero(delta)),
            "maximum_byte_error": int(delta.max(initial=0)),
            "mean_byte_error": round(float(delta.mean()), 6)}


def settle(name):
    started = time.monotonic()
    next_report = started + 5.
    frames = 0
    previous_pixels = None
    while True:
        canvas._effect_jobs.poll()
        completed_before_frame = canvas._effect_jobs.completed
        projection = getattr(canvas, "_document_projection", None)
        incomplete_before_frame = projection.incomplete if projection is not None else 0
        image = render()
        frames += 1
        app.processEvents()
        jobs = canvas._effect_jobs
        current_pixels = bytes(image.constBits()) if args.native_frame else None
        stable = (previous_pixels == current_pixels if args.native_frame else
                  not canvas._scene_dirty_full and canvas._scene_dirty_widget.isEmpty())
        # processEvents can finish a job after image capture. That capture must
        # not be declared exact merely because it matches the preceding draft.
        stable = stable and jobs.completed == completed_before_frame
        if projection is not None:
            stable = stable and projection.incomplete == incomplete_before_frame
        previous_pixels = current_pixels
        if jobs.running is None and not jobs.pending and stable:
            return image, True, (time.monotonic()-started)*1000, frames
        if time.monotonic() - started > args.settle_seconds:
            return image, False, (time.monotonic()-started)*1000, frames
        if time.monotonic() >= next_report:
            emit({"operation": name, "phase": "settling", **cache_stats()})
            next_report = time.monotonic() + 5.
        time.sleep(.005)


results = []
payload = {"method": "Complete saved chapter with all layers, masks, modifiers and images. Hidden native Qt canvas; no MainWindow, broker, Blender, settings writes or project saves. In-memory edit helpers. Timings are synchronous scene-ready CPU work, not physical input-to-display latency.",
           "label": args.label, "baseline_source_snapshot": args.baseline,
           "native_frame": args.native_frame,
           "transform_input_path": "production_update" if args.native_frame else "forced_scene_cache_invalidation",
           "chapter": chapter.name, "chapter_id": chapter.chapter_id,
           "document_size": [chapter.width, chapter.height],
           "objects": len(chapter.objects), "layers": len(chapter.layers),
           "modifiers": len(chapter.modifiers), "widget": type(canvas).__name__,
           "platform": app.platformName(), "results": results}


def checkpoint():
    (OUT / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def measure(name, action=lambda: None, *, compare=True, save=False):
    event_start, jobs_start = len(events), canvas._effect_jobs.submitted
    started = time.perf_counter()
    action()
    action_ms = (time.perf_counter() - started)*1000
    started = time.perf_counter()
    first = render()
    frame_ms = (time.perf_counter() - started)*1000
    validation_ms = getattr(render, "validation_readback_ms", 0.)
    frame_ms -= validation_ms
    first_event_end = len(events)
    first_stats = cache_stats()
    row = {"operation": name, "action_ms": round(action_ms, 3),
           "scene_ms": round(frame_ms, 3), "scene_ready_ms": round(action_ms+frame_ms, 3),
           "validation_readback_ms_excluded": round(validation_ms, 3),
           "camera": [canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation],
           "first_frame_work": event_summary(events[event_start:first_event_end]),
           "first_frame_new_jobs": canvas._effect_jobs.submitted-jobs_start,
           "first_frame_cache": first_stats}
    if compare:
        exact, settled, settle_ms, frames = settle(name)
        row.update({"settled": settled, "settle_ms": round(settle_ms, 3),
                    "settle_frames": frames, "first_vs_settled": pixel_difference(first, exact),
                    "all_work": event_summary(events[event_start:]), "final_cache": cache_stats()})
        if args.save_all or save or not settled or row["first_vs_settled"]["changed_bytes"]:
            first.save(str(OUT / f"{name}-first.png"))
            exact.save(str(OUT / f"{name}-settled.png"))
    results.append(row)
    checkpoint()
    emit(row)
    return row


try:
    emit({"phase": "loaded", **{k: v for k, v in payload.items() if k != "results"}})
    for y in (float(value) for value in args.positions.split(",") if value):
        def camera(x=540., y=y, scale=1., rotation=0.):
            canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = x, y, scale, rotation
        camera()
        measure(f"y{y:g}-cold", save=True)
        measure(f"y{y:g}-warm")
        for name, values in (("pan", {"x": 564., "y": y+16.}),
                             ("return", {}), ("tilt", {"rotation": 15.}),
                             ("untilt", {}), ("zoom-in", {"scale": 1.25}),
                             ("zoom-out", {"scale": .75}), ("restore", {})):
            measure(f"y{y:g}-{name}", lambda values=values: camera(**values))

    if args.dirty_checks:
        for name, center_y, rect in (
            ("shape-edge", 300., QRectF(630., 159., 20., 20.)),
            ("block-boundary", 1024., QRectF(500., 1018., 30., 12.)),
        ):
            canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = 540., center_y, 1., 0.
            measure(f"dirty-{name}-ready", save=True)
            expected = render()
            expected.save(str(OUT / f"dirty-{name}-before.png"))
            for repetition in (1, 2):
                row = measure(f"dirty-{name}-{repetition}",
                              lambda rect=rect: canvas._mark_scene_dirty_world(rect), save=True)
                row["dirty_world_rect"] = list(rect.getRect())
                row["unchanged_document_pixels"] = pixel_difference(expected, render())
                checkpoint()
                emit({"operation": row["operation"], "unchanged_document_pixels": row["unchanged_document_pixels"]})
                assert row["unchanged_document_pixels"]["changed_bytes"] == 0, "Unchanged artwork changed after a dirty redraw"

    if not args.skip_edits:
        obj = chapter.objects["49a55205999a431da9e5c95cb06c8bba"]
        point = QPointF(259.6838333892191, 257.1915812480561)
        canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = 540., 300., 1., 0.
        canvas.set_selection("object", obj.object_id)
        canvas.primary_color = "#FFCA24AC"
        for tool, name in ((ToolKind.RASTER_PENCIL, "draw"), (ToolKind.RASTER_ERASER, "erase")):
            canvas.set_tool(tool)
            measure(f"{name}-ready")
            measure(f"{name}-begin", lambda: canvas._begin_stroke(point, 1.))
            for n in range(1, 7):
                measure(f"{name}-segment{n}", lambda n=n: canvas._continue_stroke(point+QPointF(n*4, 0), 1.))
            measure(f"{name}-commit", canvas._end_stroke)
            measure(f"{name}-undo", canvas.command_stack.undo)
        canvas.set_tool(ToolKind.TRANSFORM)
        measure("transform-ready")
        measure("transform-begin", lambda: canvas._begin_selected_raster_transform(point))
        assert canvas._transform_drag_mode, "No transform began"
        for n in range(1, 7):
            def move(n=n):
                canvas._update_transform_preview(point+QPointF(n*3, 0))
                if args.native_frame:
                    canvas.update()
                else:
                    canvas._invalidate_scene_cache()
            measure(f"transform-segment{n}", move)
        measure("transform-commit", canvas._commit_object_transform)
        canvas._clear_transform_preview()
        measure("transform-undo", canvas.command_stack.undo)

    gpu = getattr(canvas, "_gpu_pattern_renderer", None)
    payload["gpu_pattern_available"] = bool(gpu and gpu.available)
    if gpu and gpu.available and gpu.context.makeCurrent(gpu.surface):
        payload["gpu"] = {}
        for name, token in (("vendor", 0x1F00), ("renderer", 0x1F01), ("version", 0x1F02)):
            value = gpu.functions.glGetString(token)
            payload["gpu"][name] = value.decode() if isinstance(value, bytes) else str(value)
        gpu.context.doneCurrent()
    payload["summary"] = {"operations": len(results),
        "unsettled_operations": [r["operation"] for r in results if r.get("settled") is False],
        "first_frame_changes_after_settle": [r["operation"] for r in results if r.get("first_vs_settled", {}).get("changed_bytes")],
        "median_scene_ready_ms": round(statistics.median(r["scene_ready_ms"] for r in results), 3),
        "maximum_scene_ready_ms": max(r["scene_ready_ms"] for r in results)}
finally:
    after_hashes = manifest()
    (OUT / "project-files-after.json").write_text(json.dumps(after_hashes, indent=2))
    payload["project_files_unchanged"] = before_hashes == after_hashes
    payload["project_file_count"] = len(before_hashes)
    checkpoint()
    (OUT / "events.json").write_text(json.dumps(events, indent=2))
    emit({"phase": "finished", "project_files_unchanged": before_hashes == after_hashes,
          "summary": payload.get("summary")})
    for module, name, original in reversed(originals):
        setattr(module, name, original)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    gpu = getattr(canvas, "_gpu_pattern_renderer", None)
    if gpu:
        gpu.close()
    canvas.close()
    canvas.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert before_hashes == after_hashes, "Isolated project contents changed unexpectedly"
