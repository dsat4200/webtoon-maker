"""Manual sustained tablet/paint benchmark against an isolated saved project.

Uses a hidden real MainWindow, its normal signal connections and event filters,
and a producer thread posting tablet events at fixed intended deadlines. There
is no per-move repaint, processEvents, or wait-for-effects loop. All settings,
autosaves, diagnostics and edits stay in the benchmark sandbox. Blender's
controller is replaced before MainWindow construction; no broker is started.
"""
from __future__ import annotations

import argparse
import cProfile
from collections import Counter, defaultdict
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import weakref
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts" / "drawing-stutters-20260926"
CHAPTER_ID = "0a72f08009294aa0a3d14e6a38e22bbb"
RASTER_ID = "9db9d8094e744ee4aaffb84e9db10f7c"
CAMERAS = {
    "rotated": (723.417434792031, 19631.426410948996, 2.864183112473176, -45.42587201378674),
    "lowzoom": (614.9302438479234, 19880.8243941934, .6943877037630306, 0.),
    "extreme": (-4571.983550547249, 18899.65961248689, .053351510286541996, -44.16637539018104),
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--label", required=True)
parser.add_argument("--artifact-root", type=Path,
                    help="Use a separate evidence/settings sandbox under this checkout's .artifacts.")
parser.add_argument("--include-brush", action="store_true",
                    help="Cycle Pencil, Eraser and the saved Brush preset instead of Pencil/Eraser.")
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--source-code", type=Path,
                    help="Import an explicitly preserved source checkout for before/after comparisons.")
parser.add_argument("--project-copy", type=Path, default=ARTIFACTS / "project-copy")
parser.add_argument("--scenario", choices=(*CAMERAS, 'occupied'), default="rotated")
parser.add_argument('--raster-id', default=RASTER_ID, help='Choose an existing raster in the isolated chapter.')
parser.add_argument('--camera', type=float, nargs=4, metavar=('X', 'Y', 'SCALE', 'ROTATION'),
                    help='Override the synthetic camera while retaining the chosen saved source and stacks.')
parser.add_argument('--exercise-point-chain', action='store_true',
                    help='Prepend a compatible three-effect color chain in the benchmark copy only.')
parser.add_argument('--profile', action='store_true', help='Capture GUI call costs; affects latency measurements.')
parser.add_argument('--instrument-contact', action='store_true',
                    help='Trace dirty publication and native pixel helpers; diagnostic timings only.')
parser.add_argument('--profile-startup', action='store_true', help='Include initial rendering in the GUI profile; implies --profile.')
parser.add_argument("--hz", type=float, default=120.)
parser.add_argument("--stroke-seconds", type=float, default=2.)
parser.add_argument("--strokes", type=int, default=4)
parser.add_argument("--gap-seconds", type=float, default=.4)
parser.add_argument("--drain-seconds", type=float, default=3.)
parser.add_argument("--deadline-seconds", type=float, default=90.)
parser.add_argument("--navigator", choices=("saved", "shown", "hidden"), default="saved")
parser.add_argument("--no-autosave", action="store_true")
parser.add_argument("--navigate-between-strokes", action="store_true")
parser.add_argument("--cold-only", action="store_true")
parser.add_argument("--async-exact", action="store_true",
                    help="Start input before first exposure, await exact readiness, and validate final native pixels.")
parser.add_argument("--effect-workers", type=int, choices=range(1, 5),
                    help="Limit detached effect concurrency for matched diagnostic runs.")
parser.add_argument("--async-warm-start", action="store_true",
                    help="With async-exact, wait for the initial exact view before sustained input/navigation.")
parser.add_argument('--input-after-exposure', action='store_true',
                    help='Start cold input after the first native paint, without waiting for source/scene readiness.')
parser.add_argument("--warm-from", choices=CAMERAS,
                    help="Warm this camera outside measurement, then measure an exact jump to scenario (cold-only).")
parser.add_argument("--no-stroke-warmup", action="store_true",
                    help="Skip explicit initial repaint; Qt may already have painted its exposure.")
args = parser.parse_args()
if args.artifact_root is not None:
    ARTIFACTS = args.artifact_root.resolve()
    assert ARTIFACTS.is_relative_to((ROOT / '.artifacts').resolve())
RASTER_ID = args.raster_id
assert not (args.baseline and args.source_code)
assert 1 <= args.hz <= 1000 and 0 < args.stroke_seconds <= 30 and 1 <= args.strokes <= 20
assert not args.async_warm_start or args.async_exact
assert not args.input_after_exposure or (args.async_exact and not args.async_warm_start)
assert not args.warm_from or (args.async_exact and args.cold_only)
if args.warm_from:
    args.async_warm_start = True
OUT = (ARTIFACTS / args.label).resolve()
COPY = args.project_copy.resolve()
assert OUT.is_relative_to(ARTIFACTS.resolve()) and OUT != ARTIFACTS.resolve()
assert COPY.is_relative_to((ROOT / ".artifacts").resolve()) and (COPY / "series.json").is_file()
assert not OUT.exists(), "Use a new label to preserve previous measurements"
OUT.mkdir(parents=True)
shutil.copyfile(ARTIFACTS / "user-settings-snapshot.json", OUT / "settings.json")
if (ARTIFACTS / 'brush-assets').is_dir():
    shutil.copytree(ARTIFACTS / 'brush-assets', OUT / 'brush-assets')
sys.path.insert(0, str(args.source_code.resolve() if args.source_code else ARTIFACTS / "baseline-source" if args.baseline else ROOT))
os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["QT_TLS_BACKEND"] = "schannel"

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase, QImage, QInputDevice, QPointingDevice, QSurfaceFormat, QTabletEvent, QTransform, QWheelEvent
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings as settings_module
from comic_editor.core.models import SeriesDocument
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui import canvas as canvas_module, main_window as window_module
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.preview import ChapterPreview
from comic_editor.ui import distort_rendering, effect_pipeline, interactive_effects, modifier_rendering
from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer
from drawing_benchmark_support import (ExactProgress, canvas_pending, canvas_signature,
                                       pixel_difference, read_native_frame)


def manifest():
    return {str(p.relative_to(COPY)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(COPY.rglob("*")) if p.is_file()}


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2), encoding="utf-8")


def distribution(values):
    if not values:
        return {"count": 0}
    return {"count": len(values), "total_ms": float(sum(values)),
            "p50_ms": float(np.percentile(values, 50)), "p95_ms": float(np.percentile(values, 95)),
            "p99_ms": float(np.percentile(values, 99)), "max_ms": float(max(values)),
            "over_50ms": sum(v > 50 for v in values), "over_250ms": sum(v > 250 for v in values)}


class DisconnectedBlender(QObject):
    """UI signal shape only: never construct the integration/controller/client."""
    viewsChanged = Signal(object)
    connectionStateChanged = Signal(str)
    providerInfoChanged = Signal(object)
    statusChanged = Signal(str)
    switchDecisionRequired = Signal(object)
    frameImported = Signal()
    errorOccurred = Signal(str)

    def __init__(self, canvas, parent):
        super().__init__(parent)
        self.client = SimpleNamespace(connected=False, refresh_views=lambda: None,
                                      resolve_dirty_switch=lambda *_: None)

    def handle_selection(self, *_):
        pass

    disconnect = shutdown = stop_for_context_change = resume_for_context = handle_selection


class Probe:
    def __init__(self):
        self.phase = "setup"
        self.activity = "setup"
        self.started = time.perf_counter()
        self.rows = []
        self.samples = defaultdict(list)
        self.patches = []
        self.frames = []
        self.controls = []
        self.input_rows = []
        self.input_by_stamp = {}
        self.pending_inputs = []
        self.pending_exact_inputs = []
        self.exact_progress = ExactProgress()
        self.presentation_started = None
        self.exposure_requested_at = self.first_native_paint_at = None
        self.input_start = None
        self.signals = Counter()
        self.last_presented_sequence = -1
        self.feedback_tiles = 0
        self.feedback_contact_covered = False
        self.gui_thread = threading.get_ident()
        self.local = threading.local()

    def patch(self, owner, name, label):
        if not hasattr(owner, name):
            return
        original = getattr(owner, name)
        @wraps(original)
        def measured(*values, **kwargs):
            start, phase, activity = time.perf_counter(), self.phase, self.activity
            previous_entity = getattr(self.local, "entity", None)
            entity = values[2] if label in {"canvas.modified_object", "canvas.modified_layer", "canvas.mirror"} and len(values) > 2 else None
            if entity is not None:
                self.local.entity = {"id": getattr(entity, "object_id", getattr(entity, "layer_id", None)),
                                     "name": getattr(entity, "name", None)}
            entity_info = getattr(self.local, "entity", None)
            distance_before = getattr(values[0], "computations", None) if values else None
            result = None
            try:
                result = original(*values, **kwargs)
                return result
            finally:
                end = time.perf_counter()
                self.local.entity = previous_entity
                if phase != "setup":
                    ms = (end - start) * 1000
                    self.samples[(phase, label)].append(ms)
                    row = {"phase": phase, "activity": activity, "name": label, "start_ms": (start-self.started)*1000,
                           "ms": ms, "thread": threading.get_ident(),
                           "gui_thread": threading.get_ident() == self.gui_thread}
                    if entity_info:
                        row["entity"] = entity_info
                    modifiers = []
                    for value in values:
                        candidates = value if isinstance(value, (tuple, list)) else (value,)
                        for candidate in candidates:
                            if hasattr(candidate, "modifier_type"):
                                modifiers.append({"id": candidate.modifier_id, "type": candidate.modifier_type})
                    if modifiers and (label != 'effects.fused_points' or result is not None):
                        row["modifiers"] = modifiers
                    source = next((v for v in values if isinstance(v, QImage)), None)
                    if source is not None:
                        row["source_size"] = [source.width(), source.height()]
                    output = result if isinstance(result, QImage) else getattr(result, "image", None)
                    if isinstance(output, QImage):
                        row["output_size"] = [output.width(), output.height()]
                    array = next((v for v in values if isinstance(v, np.ndarray)), None)
                    if array is not None:
                        row["array_shape"] = list(array.shape)
                    if label == "effects.outline_field" and distance_before is not None:
                        row["distance_builds"] = values[0].computations-distance_before
                    if label == "canvas.promoted_ink":
                        row["result"] = bool(result)
                    if label == "canvas.raster_feedback":
                        self.feedback_tiles = int(result or 0)
                        self.feedback_contact_covered = bool(getattr(
                            values[0].canvas, '_raster_feedback_contact_covered', False))
                        row["result"] = self.feedback_tiles
                        row['current_contact_covered'] = self.feedback_contact_covered
                    self.rows.append(row)
                    if label == "canvas.paint":
                        self.frame(end, ms, phase, activity)
        setattr(owner, name, measured)
        self.patches.append((owner, name, original))

    def frame(self, ended, ms, phase, activity):
        pending, self.pending_inputs = self.pending_inputs, []
        frame = {"phase": phase, "activity": activity, "end_ms": (ended-self.started)*1000, "paint_ms": ms,
                 "newly_dispatched_inputs": len(pending), "raster_feedback_tiles": self.feedback_tiles,
                 "current_raster_contact_covered": self.feedback_contact_covered}
        self.feedback_tiles = 0
        self.feedback_contact_covered = False
        if pending:
            frame.update(oldest_input_to_paint_ms=(ended-pending[0]["scheduled_at"])*1000,
                         newest_input_to_paint_ms=(ended-pending[-1]["scheduled_at"])*1000,
                         last_sequence=pending[-1]["sequence"])
            self.last_presented_sequence = pending[-1]["sequence"]
        if args.async_exact:
            state = canvas_pending(canvas)
            ready = self.exact_progress.observe(
                ended, frame_pending=state["frame_pending"], jobs_busy=state["jobs_busy"],
                failed=state["failed"], signature=canvas_signature(canvas), activity=activity)
            frame["exact_state"] = state
            frame["exact_ready"] = ready
            if ready and self.pending_exact_inputs:
                frame["oldest_input_to_exact_paint_ms"] = (
                    ended-self.pending_exact_inputs[0]["scheduled_at"])*1000
                frame["last_exact_sequence"] = self.pending_exact_inputs[-1]["sequence"]
                for row in self.pending_exact_inputs:
                    row["scheduled_to_exact_paint_ms"] = (ended-row["scheduled_at"])*1000
                self.pending_exact_inputs.clear()
        self.frames.append(frame)
        if self.first_native_paint_at is None:
            self.first_native_paint_at = ended
            if args.input_after_exposure:
                QTimer.singleShot(0, start_producer)


settings_module.settings_path = lambda: OUT / "settings.json"
window_module.BlenderImageSourceController = DisconnectedBlender
window_module.MainWindow._clipboard_data_changed = lambda self: None
QCoreApplication.setAttribute(Qt.AA_CompressTabletEvents, False)
QCoreApplication.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTabletEvents, True)
surface = QSurfaceFormat()
surface.setRenderableType(QSurfaceFormat.OpenGL)
surface.setVersion(3, 3)
surface.setProfile(QSurfaceFormat.CoreProfile)
surface.setSamples(0)
surface.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(surface)
app = QApplication([])
app.setApplicationName("Drawing benchmark")
app.setOrganizationName("Isolated benchmark")
for name in ("arial.ttf", "segoeui.ttf", "comic.ttf", "times.ttf"):
    QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name))
probe = Probe()
# Patch classes before construction so signal-bound ancillary callbacks are timed.
for name in ("_mark_dirty", "_command_stack_changed", "_refresh_actions", "_refresh_project_tabs",
             "_sync_contextual_ribbon", "_sync_tool_buttons", "_canvas_selection_changed",
             "_canvas_tool_changed", "_refresh_masks_panel", "_refresh_hierarchy", "_autosave"):
    probe.patch(window_module.MainWindow, name, "window." + name.lstrip("_"))
for name, label in (
    ("paintEvent", "paint"), ("tabletEvent", "tablet"), ("_begin_stroke", "begin"),
    ("_continue_stroke", "move"), ("_end_stroke", "end"), ("set_solo_entities", "solo"),
    ("_render_document_region", "projection_region"), ("_collect_document_projection", "projection_collect"),
    ("_invalidate_scene_cache", "invalidate_all"), ("_mark_scene_dirty_world", "invalidate_region"),
    ("_render_modified_object", "modified_object"), ("_render_modified_layer", "modified_layer"),
    ("_render_mirror_target", "mirror"), ("render_preview", "preview"),
    ("_modifier_source_cache_put", "source_capture"),
    ("_show_on_top_live_ink", "promoted_ink"), ("_render_scene_cache_rect", "legacy_scene_rect"),
    ("_paint_document_projection", "projection_paint"), ("_draw_predictive_ink", "predictive_ink"),
    ("_update_navigation", "navigation_update"), ("wheelEvent", "wheel"),
):
    probe.patch(canvas_module._CanvasLogic, name, "canvas." + label)
probe.patch(ChapterPreview, "_render_live_preview", "navigator.render")
probe.patch(distort_rendering, "render_distort", "effects.distort")
probe.patch(effect_pipeline, "apply_modifier_stack", "effects.exact_stack")
probe.patch(interactive_effects, "apply_modifier_stack", "effects.interactive_stack")
import importlib
import importlib.util
for module_name, function_name, label in (
    ('tile_effects', 'apply_modifier_stack', 'effects.tile_stack'),
    ('point_lut', 'point_chain', 'effects.fused_points'),
):
    full_name = f'comic_editor.ui.{module_name}'
    if importlib.util.find_spec(full_name) is not None:
        probe.patch(importlib.import_module(full_name), function_name, label)
probe.patch(GpuPatternRenderer, "render", "effects.pattern")
probe.patch(modifier_rendering, "_outside_distance", "effects.outline_distance")
probe.patch(modifier_rendering, "_outline_qimage", "effects.outline")
probe.patch(modifier_rendering.OutlineDistanceCache, "field", "effects.outline_field")
if args.instrument_contact:
    from comic_editor.core.tiles import TileStore
    for name in ('_emit_raster_dirty', '_publish_change_set', '_queue_visual_dirty',
                 'modifier_expanded_dirty', '_invalidate_render_bounds'):
        probe.patch(canvas_module._CanvasLogic, name, 'contact.' + name.lstrip('_'))
    for name in ('paint_segment', '_paint_samples'):
        probe.patch(TileStore, name, 'contact.tiles.' + name)
    probe.patch(window_module.MainWindow, '_canvas_changes_published', 'contact.window_changes')
try:
    from comic_editor.ui import tile_input
except ImportError:
    pass
else:
    probe.patch(tile_input, 'prepare_input_tiles', 'sources.native_input')
    probe.patch(tile_input, 'prepare_input_bounds', 'sources.native_bounds')

before_hashes = manifest()
write_json("project-files-before.json", before_hashes)
repository = SeriesRepository(COPY)
chapter, tiles, images = repository.load_chapter(CHAPTER_ID, include_images=True)
assert RASTER_ID in chapter.objects
forced_visible_layers = []
if args.scenario == 'occupied':
    # Saved drafting pages may be hidden. A stroke on one would otherwise
    # measure pointer dispatch without ever evaluating its drawing effects.
    chapter.objects[RASTER_ID].visible = True
    ancestor = chapter.objects[RASTER_ID].parent_layer_id
    while ancestor is not None:
        layer = chapter.layers[ancestor]
        if not layer.visible:
            forced_visible_layers.append(ancestor)
            layer.visible = True
        ancestor = layer.parent_id
if args.exercise_point_chain:
    from comic_editor.core.models import BrightnessContrastModifier, CurvesModifier
    point_effects = [BrightnessContrastModifier(brightness=13.25, contrast=23.5, intensity=57.75),
                    CurvesModifier(intensity=83.25, curves={'rgb:master': [[0,0],[.35,.65],[1,1]]}),
                    BrightnessContrastModifier(brightness=-21.25, contrast=-30.5, intensity=66.75)]
    chapter.modifiers.update({modifier.modifier_id: modifier for modifier in point_effects})
    chapter.objects[RASTER_ID].modifier_ids[:0] = [modifier.modifier_id for modifier in point_effects]
window = window_module.MainWindow()
window.setAttribute(Qt.WA_DontShowOnScreen, True)
window.setAttribute(Qt.WA_ShowWithoutActivating, True)
canvas = window.canvas
if args.instrument_contact:
    probe.patch(window.selection_settings, 'refresh', 'contact.selection_properties')
    probe.patch(window.hierarchy_model, 'apply_change', 'contact.hierarchy_rows')
if getattr(canvas, '_scene_controller', None) is not None:
    probe.patch(type(canvas._scene_controller), 'present_feedback', 'canvas.raster_feedback')
if args.effect_workers is not None:
    available_workers = getattr(canvas._effect_jobs, 'worker_limit', 1)
    assert args.effect_workers <= available_workers, 'Requested concurrency exceeds the configured pool'
    if hasattr(canvas._effect_jobs, 'worker_limit'):
        canvas._effect_jobs.worker_limit = args.effect_workers
canvas._projection_async_enabled = bool(args.async_exact and not args.baseline)
canvas.setFixedSize(955, 927)
window.series = SeriesDocument.from_dict(json.loads((COPY / "series.json").read_text()))
if not args.no_autosave:
    window.repository = SeriesRepository(OUT / "sandbox-saves")
    window.repository.create("Isolated benchmark autosaves")
window._set_chapter(chapter, tiles, images)
canvas.set_selection("object", RASTER_ID)
window._activate_tool(ToolKind.RASTER_PENCIL)
input_history_revision = canvas.command_stack.revision
if args.scenario == 'occupied':
    occupied = canvas.object_world_rect(RASTER_ID)
    assert occupied is not None and not occupied.isEmpty()
    center = occupied.center()
    density = min(.7, 955 / max(1, occupied.width()*1.1), 927 / max(1, occupied.height()*1.1))
    CAMERAS['occupied'] = (center.x(), center.y(), max(.05, density), 0.)
if args.camera is not None:
    assert .05 <= args.camera[2] <= 8. and all(math.isfinite(value) for value in args.camera)
    CAMERAS[args.scenario] = tuple(args.camera)
canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = CAMERAS[args.warm_from or args.scenario]
if args.navigator != "saved":
    window.navigator_panel.setExpanded(args.navigator == "shown", emit=False)
window.resize(1600, 1100)
probe.phase = probe.activity = "cold"
if not args.async_exact:
    window.show()
for name in ("visualChanged", "documentChanged", "interactionFinished", "cameraChanged",
             "selectionChanged", "toolChanged", "soloChanged"):
    getattr(canvas, name).connect(lambda *_a, name=name: probe.signals.update([name]))

control_type = QEvent.Type(QEvent.registerEventType())
pen = QPointingDevice("Benchmark pen", 1, QInputDevice.DeviceType.Stylus,
                     QPointingDevice.PointerType.Pen,
                     QInputDevice.Capability.Position | QInputDevice.Capability.Pressure, 1, 3)
producer_done = threading.Event()
stop_producer = threading.Event()
heartbeat_gaps = []
heartbeat_samples = []
last_heartbeat = time.perf_counter()
input_end = None
input_started = None
timed_out = False
schedule = []
rect = canvas.object_world_rect(RASTER_ID)
assert rect and not rect.isEmpty()
# This is a deterministic synthetic path near the recorded camera, not a replay
# of unavailable pen coordinates. Preserve all generated coordinates in JSON.
planned_camera = list(CAMERAS[args.scenario])
sequence = 0
offset = 0.


def add_navigation(start, outward):
    """Plan real navigation endpoint calls, including normal camera snapping."""
    center = QPointF(canvas.rect().center())
    sign = 1 if outward else -1
    pan_delta = QPointF(120 * sign, 0)
    # Inverse order restores the same neighborhood after every second stroke.
    operations = ("pan", "rotate", "wheel") if outward else ("wheel", "rotate", "pan")
    for operation in operations:
        if operation == "wheel":
            for i in range(2):
                schedule.append({"kind": "wheel", "offset": start+i*.09,
                                 "angle": 120*sign, "activity": "navigation"})
            for _ in range(2):
                planned_camera[2] = max(.05, min(8., planned_camera[2]*math.pow(1.0015, 120*sign)))
            start += .22
            continue
        anchor_point = center if operation == "pan" else center+QPointF(180, 0)
        schedule.append({"kind": "nav_begin", "offset": start, "mode": operation,
                         "point": anchor_point.toTuple(), "activity": "navigation"})
        for step in range(1, 13):
            fraction = step/12
            if operation == "pan":
                point = anchor_point + pan_delta*fraction
            else:
                angle = math.radians(15*sign*fraction)
                point = center+QPointF(180*math.cos(angle), 180*math.sin(angle))
            schedule.append({"kind": "nav_update", "offset": start+fraction*.20,
                             "point": point.toTuple(), "activity": "navigation"})
        schedule.append({"kind": "nav_end", "offset": start+.205,
                         "point": point.toTuple(), "activity": "navigation"})
        if operation == "pan":
            angle = math.radians(-planned_camera[3])
            planned_camera[0] -= pan_delta.x()*math.cos(angle)/planned_camera[2]
            planned_camera[1] -= pan_delta.x()*math.sin(angle)/planned_camera[2]
        else:
            planned_camera[3] += 15*sign
        planned_camera[0] = round(planned_camera[0]*planned_camera[2])/planned_camera[2]
        planned_camera[1] = round(planned_camera[1]*planned_camera[2])/planned_camera[2]
        start += .23
    return start+.05


for stroke in range(0 if args.cold_only else args.strokes):
    if stroke and args.navigate_between_strokes:
        offset = add_navigation(offset, outward=bool(stroke % 2))
    tools = (ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH) if args.include_brush else (
        ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER)
    tool = tools[stroke % len(tools)]
    schedule.append({"kind": "tool", "offset": offset, "tool": tool,
                     "activity": f"stroke-{stroke}", "expected_camera": list(planned_camera)})
    anchor = QPointF(max(rect.left()+30, min(rect.right()-30, planned_camera[0])),
                     max(rect.top()+30, min(rect.bottom()-30, planned_camera[1])))
    if args.scenario == 'occupied':
        # The center pivot handle owns a press within a fixed screen radius.
        # Use an interior ink position away from it and the corner handles.
        anchor = QPointF(rect.left() + rect.width()*.37, rect.top() + rect.height()*.43)
        if args.camera is not None:
            anchor = QPointF(max(rect.left()+30, min(rect.right()-30, planned_camera[0])),
                             max(rect.top()+30, min(rect.bottom()-30, planned_camera[1])))
    transform = QTransform()
    transform.translate(canvas.width()/2, canvas.height()/2)
    transform.rotate(planned_camera[3])
    transform.scale(planned_camera[2], planned_camera[2])
    transform.translate(-planned_camera[0], -planned_camera[1])
    count = max(2, round(args.stroke_seconds * args.hz))
    for point in range(count+2):
        fraction = min(1., point / count)
        world = anchor + QPointF((fraction-.5)*70, math.sin(fraction*math.pi*4)*8)
        position = transform.map(world)
        event_kind = "press" if point == 0 else "release" if point == count+1 else "move"
        schedule.append({"kind": event_kind, "offset": offset + fraction*args.stroke_seconds + .015,
                         "sequence": sequence, "position": position.toTuple(),
                         "global_position": canvas.mapToGlobal(position.toPoint()).toTuple(),
                         "stroke": stroke, "after_navigation": bool(stroke and args.navigate_between_strokes),
                         "world": world.toTuple(), "pressure": 0. if event_kind == "release" else .7})
        sequence += 1
    offset += args.stroke_seconds+args.gap_seconds


class ControlEvent(QEvent):
    def __init__(self, action):
        super().__init__(control_type)
        self.action = action


class Driver(QObject):
    def event(self, event):
        if event.type() == control_type:
            action = event.action
            probe.activity = action["activity"]
            start = time.perf_counter()
            kind = action["kind"]
            if kind == "tool":
                window._activate_tool(action["tool"])
            elif kind == "nav_begin":
                canvas._begin_navigation(action["mode"], QPointF(*action["point"]))
            elif kind == "nav_update":
                canvas._queue_navigation_update(QPointF(*action["point"]))
            elif kind == "nav_end":
                canvas._end_navigation(QPointF(*action["point"]))
            elif kind == "wheel":
                point = QPointF(canvas.rect().center())
                wheel = QWheelEvent(point, QPointF(canvas.mapToGlobal(point.toPoint())),
                                    QPoint(), QPoint(0, action["angle"]), Qt.NoButton, Qt.ControlModifier,
                                    Qt.ScrollPhase.NoScrollPhase, False)
                QCoreApplication.sendEvent(canvas, wheel)
            probe.controls.append({"kind": kind, "activity": probe.activity,
                                   "dispatch_ms": (start-probe.started)*1000,
                                   "queue_ms": (start-action["posted_at"])*1000,
                                   "scheduled_delay_ms": (start-action["scheduled_at"])*1000,
                                   "handler_ms": (time.perf_counter()-start)*1000,
                                   "camera": [canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation],
                                   "expected_camera": action.get("expected_camera")})
            return True
        return super().event(event)

    def eventFilter(self, watched, event):
        if watched is canvas and event.type() in (QEvent.TabletPress, QEvent.TabletMove, QEvent.TabletRelease):
            row = probe.input_by_stamp.get(event.timestamp())
            if row is not None:
                now = time.perf_counter()
                row["dispatched_at"] = now
                row["scheduled_to_dispatch_ms"] = (now-row["scheduled_at"])*1000
                row["posted_to_dispatch_ms"] = (now-row["posted_at"])*1000
                probe.pending_inputs.append(row)
                if args.async_exact:
                    probe.pending_exact_inputs.append(row)
        return False


driver = Driver()
app.installEventFilter(driver)


def produce():
    base = time.perf_counter() + .050
    types = {"press": QEvent.TabletPress, "move": QEvent.TabletMove, "release": QEvent.TabletRelease}
    try:
        for item in schedule:
            due = base + item["offset"]
            if stop_producer.wait(max(0., due-time.perf_counter())):
                return
            if item["kind"] not in types:
                QCoreApplication.postEvent(driver, ControlEvent({**item, "scheduled_at": due,
                                                                "posted_at": time.perf_counter()}))
                continue
            position = QPointF(*item["position"])
            button = Qt.NoButton if item["kind"] == "move" else Qt.LeftButton
            buttons = Qt.NoButton if item["kind"] == "release" else Qt.LeftButton
            event = QTabletEvent(types[item["kind"]], pen, position, QPointF(*item["global_position"]), item["pressure"],
                                 0., 0., 0., 0., 0., Qt.NoModifier, button, buttons)
            stamp = item["sequence"] + 1
            event.setTimestamp(stamp)
            posted = time.perf_counter()
            row = {**item, "scheduled_at": due, "posted_at": posted,
                   "producer_late_ms": (posted-due)*1000}
            probe.input_rows.append(row)
            probe.input_by_stamp[stamp] = row
            QCoreApplication.postEvent(canvas, event)
    finally:
        producer_done.set()


def graphics_state():
    point_module = sys.modules.get('comic_editor.ui.point_lut')
    worker = getattr(point_module, '_worker', None)
    if worker is not None:
        return {'available': worker.available, 'reason': worker.reason,
                'ready': worker.ready.is_set(), 'closed': worker.closed,
                'queued_bytes': worker.queued_bytes, **worker.stats}


def native_graphics():
    import ctypes
    canvas.makeCurrent()
    try:
        gl = ctypes.WinDLL('opengl32')
        gl.glGetString.restype = ctypes.c_char_p
        return gl.glGetString(0x1F01).decode()
    finally:
        canvas.doneCurrent()


graphics_before_shutdown = None
def finish_run():
    global graphics_before_shutdown
    graphics_before_shutdown = graphics_state()
    app.quit()


def heartbeat():
    global last_heartbeat, input_end, timed_out
    now = time.perf_counter()
    heartbeat_gaps.append((now-last_heartbeat)*1000)
    heartbeat_samples.append({"phase": probe.phase, "activity": probe.activity,
                              "at_ms": (now-probe.started)*1000,
                              "gap_ms": (now-last_heartbeat)*1000})
    last_heartbeat = now
    if (producer_done.is_set() and input_end is None
            and all("dispatched_at" in row for row in probe.input_rows)
            and not canvas._drawing):
        input_end = now
        probe.phase = "drain"
    state = canvas_pending(canvas) if args.async_exact else None
    if (args.async_warm_start and thread is None and state is not None
            and probe.exact_progress.terminal(
                now, last_activity_at=probe.presentation_started or probe.started,
                signature=canvas_signature(canvas), jobs_busy=state["jobs_busy"],
                failed=state["failed"])):
        start_producer()
    if state and state["failed"]:
        finish_run()
    elif now-probe.started > args.deadline_seconds:
        timed_out = True
        finish_run()
    elif input_end is not None and now-input_end >= args.drain_seconds:
        last_activity = max(
            [r["dispatched_at"] for r in probe.input_rows if "dispatched_at" in r]
            + [probe.started + r["dispatch_ms"]/1000 for r in probe.controls]
            + [probe.presentation_started or probe.started])
        if not args.async_exact or probe.exact_progress.terminal(
                now, last_activity_at=last_activity, signature=canvas_signature(canvas),
                jobs_busy=state["jobs_busy"], failed=state["failed"]):
            finish_run()


thread = None
def start_producer():
    global thread, input_started, last_heartbeat
    if thread is not None:
        return
    if args.profile and args.async_warm_start and gui_profile is not None:
        gui_profile.enable()
    if args.warm_from:
        write_json("warmup.json", {
            "camera": CAMERAS[args.warm_from], "elapsed_ms": (time.perf_counter()-probe.started)*1000,
            "frames": probe.frames, "terminal": canvas_pending(canvas),
        })
        probe.rows.clear()
        probe.samples.clear()
        probe.frames.clear()
        probe.controls.clear()
        probe.signals.clear()
        heartbeat_gaps.clear()
        heartbeat_samples.clear()
        probe.exact_progress = ExactProgress()
        probe.started = probe.presentation_started = last_heartbeat = time.perf_counter()
        canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = CAMERAS[args.scenario]
        canvas.cameraChanged.emit()
        canvas.update()
        probe.controls.append({"kind": "warm_from_target", "activity": "navigation",
                               "dispatch_ms": 0., "camera": list(CAMERAS[args.scenario]),
                               "expected_camera": list(CAMERAS[args.scenario])})
    input_started = time.perf_counter()
    source = canvas.tiles._tiles.get(RASTER_ID)
    controller = getattr(canvas, '_scene_controller', None)
    source_evictions = 0
    if args.input_after_exposure and source is not None:
        # Project setup can establish native alpha bounds by loading sources.
        # This explicit cold case retires only the selected original borrowers;
        # native backings, versions and every other source remain unchanged.
        versions = {key: source.version(key) for key in source}
        for key in source:
            if (source, key) in canvas.tiles.residency.entries:
                canvas.tiles.residency.evict(source, key)
                source_evictions += 1
        assert versions == {key: source.version(key) for key in source}
    probe.input_start = {
        'policy': 'after-first-native-paint' if args.input_after_exposure else
                  'after-exact-readiness' if args.async_warm_start else 'before-native-exposure',
        'at_ms': (input_started-probe.started)*1000,
        'target_source_tiles': len(source) if source is not None else 0,
        'target_source_evictions': source_evictions,
        'target_resident_tiles': sum((source, key) in canvas.tiles.residency.entries
                                     for key in source) if source is not None else 0,
        'live_tile_decodes': canvas.tiles.residency.decodes,
        'scene_state': canvas_pending(canvas),
        'target_feedback_prepared': bool(controller is not None and getattr(controller, 'feedback', None) is not None),
        'native_source_preparation_busy': any(gate.busy for gate in canvas._native_input_gates())
            if hasattr(canvas, '_native_input_gates') else False,
    }
    probe.phase = "cold" if args.cold_only else "input"
    thread = threading.Thread(target=produce, name="synthetic-tablet-producer", daemon=True)
    thread.start()


def start():
    global thread, last_heartbeat
    probe.phase = probe.activity = "cold"
    if not args.async_exact and not args.no_stroke_warmup:
        canvas.repaint()
    if not args.async_exact:
        assert canvas.isValid(), "A real hidden OpenGL canvas is required"
    probe.phase = "cold" if args.cold_only or args.async_warm_start else "input"
    last_heartbeat = time.perf_counter()
    timer.start()
    probe.presentation_started = time.perf_counter()
    if not args.async_warm_start and not args.input_after_exposure:
        start_producer()
    if args.async_exact:
        probe.exposure_requested_at = time.perf_counter()
        window.show()
        assert canvas.isValid(), "A real hidden OpenGL canvas is required"


def deadline():
    global timed_out
    timed_out = True
    finish_run()


timer = QTimer()
timer.setTimerType(Qt.PreciseTimer)
timer.setInterval(20)
timer.timeout.connect(heartbeat)
write_json("setup.json", {
    "source": str(Path(canvas_module.__file__).resolve()), "project": str(COPY),
    "gui_thread": probe.gui_thread,
    "scenario": args.scenario, "camera": CAMERAS[args.scenario], "selected": RASTER_ID,
    "forced_visible_layers_in_memory": forced_visible_layers,
    "chapter": {"objects": len(chapter.objects), "modifiers": len(chapter.modifiers),
                "layers": len(chapter.layers), "masks": len(chapter.masks)},
    "viewport": [955, 927], "settings": json.loads((OUT / "settings.json").read_text()),
    "device_pixel_ratio": canvas.devicePixelRatioF(),
    "effect_workers": getattr(canvas._effect_jobs, 'worker_limit', 1),
    "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
    'input_start_policy': 'first completed native paint; source and scene readiness are not awaited'
        if args.input_after_exposure else 'initial exact readiness' if args.async_warm_start
        else 'producer starts before native exposure',
    "limitations": ["Synthetic pen trajectory and constant pressure; no original raw pen samples exist.",
                    "Input deadlines include producer GIL delay; posted queue delay is reported separately.",
                    "Frame completion is Qt paint completion, not physical display presentation.",
                    "No OS tablet driver, touch gestures or modal selection-menu replay.",
                    "Navigation uses the actual drag-navigation methods and Ctrl-wheel events on a scripted path.",
                    "Blender replaced with a nonconnecting stub; autosaves go only to output sandbox."],
})
source_root = Path(canvas_module.__file__).resolve().parents[1]
write_json("source-files.json", {
    "source_root": str(source_root),
    "python_files": {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(source_root.rglob("*.py"))},
    "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
})
try:
    QTimer.singleShot(round(args.deadline_seconds * 1000), deadline)
    QTimer.singleShot(0, start)
    gui_profile = cProfile.Profile() if args.profile or args.profile_startup else None
    if gui_profile is not None and (args.profile_startup or not args.async_warm_start):
        gui_profile.enable()
    app.exec()
finally:
    if 'gui_profile' in globals() and gui_profile is not None:
        gui_profile.disable()
        gui_profile.dump_stats(str(OUT/'gui.prof'))
    stop_producer.set()
    if thread is not None:
        thread.join(2.)
    timer.stop()
    probe.phase = "shutdown"
    measured_projection = canvas._document_projection.snapshot()
    preparation_before_shutdown = None
    pipeline_module = sys.modules.get('comic_editor.ui.distort_pipeline')
    registry = getattr(pipeline_module, '_worker_preparation', None)
    shared_preparation = registry() if isinstance(registry, weakref.ReferenceType) else None
    if shared_preparation is not None:
        preparation_before_shutdown = {
            'pool_budget': shared_preparation.budget,
            'pool_bytes': shared_preparation.bytes,
            'entries': len(shared_preparation._entries),
        }
    shared_preparation = None
    exact_status = None
    if args.async_exact:
        state = canvas_pending(canvas)
        progress = probe.exact_progress
        last_activity = max(
            [r["dispatched_at"] for r in probe.input_rows if "dispatched_at" in r]
            + [probe.started + r["dispatch_ms"]/1000 for r in probe.controls]
            + [probe.presentation_started or probe.started])
        ready = progress.terminal(time.perf_counter(), last_activity_at=last_activity,
                                  signature=canvas_signature(canvas),
                                  jobs_busy=state["jobs_busy"], failed=state["failed"])
        origin = probe.presentation_started or probe.started
        exact_status = {
            "terminal_ready": ready, "terminal_state": state,
            "first_paint_ms": ((progress.first_paint_at-origin)*1000
                               if progress.first_paint_at is not None else None),
            "first_exact_ready_ms": ((progress.first_exact_at-origin)*1000
                                     if progress.first_exact_at is not None else None),
            "terminal_exact_ready_ms": ((progress.ready_since-origin)*1000
                                        if progress.ready_since is not None else None),
            "input_started_ms": ((input_started-origin)*1000 if input_started is not None else None),
            "input_finished_before_first_exact": bool(input_end is not None and
                (progress.first_exact_at is None or input_end < progress.first_exact_at)),
            "input_to_exact_paint": distribution([r["oldest_input_to_exact_paint_ms"]
                for r in probe.frames if "oldest_input_to_exact_paint_ms" in r]),
            "each_input_to_exact_paint": distribution([r["scheduled_to_exact_paint_ms"]
                for r in probe.input_rows if "scheduled_to_exact_paint_ms" in r]),
            "unpresented_exact_inputs": len(probe.pending_exact_inputs),
            "transitions": [{**row, "at_ms": (row["at"]-origin)*1000}
                            for row in progress.transitions],
        }
        if ready and not timed_out:
            # Deliberate validation readbacks and synchronous oracle work are
            # outside the timed event loop. Capture the finished async frame
            # before invalidating anything, so a stale presentation cannot be
            # repaired silently by the oracle before it is compared.
            probe.phase = "validation"
            validation_started = time.perf_counter()
            finished = read_native_frame(canvas)
            finished.save(str(OUT / "finished-async-native.png"))
            controller = getattr(canvas, '_scene_controller', None)
            projection = canvas._document_projection
            backing_callbacks = projection.backing_lookup, projection.backing_retain
            if controller is not None:
                controller.reset()
                # This validation deliberately uses the explicit synchronous
                # widget oracle. Production ready presentation must not queue
                # another detached capture while that oracle is measured.
                canvas._scene_controller = None
            try:
                canvas._projection_async_enabled = False
                canvas._projection_cull_outside_view = False
                # Production backing lookup defers source verification/reads.
                # This independent synchronous oracle must evaluate every
                # native tile rather than retain previous coverage on a miss.
                projection.backing_lookup = projection.backing_retain = None
                canvas._effect_jobs.cancel()
                canvas._modifier_render_cache.clear()
                canvas._modifier_render_cache_bytes = 0
                canvas._modifier_source_cache.clear()
                canvas._modifier_source_cache_bytes = 0
                canvas._distort_preparation_cache = None
                canvas._document_projection.clear()
                canvas._invalidate_scene_cache()
                canvas._ensure_scene_cache()
                QImage(canvas._scene_cache).save(str(OUT / "synchronous-scene.png"))
                canvas.repaint()
                reference = read_native_frame(canvas)
                reference.save(str(OUT / "synchronous-native.png"))
                exact_status["oracle_pending"] = canvas_pending(canvas)
                assert not exact_status["oracle_pending"]["frame_pending"], (
                    "Synchronous oracle did not finish its native frame", exact_status["oracle_pending"])
                assert not exact_status["oracle_pending"]["jobs_busy"], (
                    "Synchronous oracle deferred effect work", exact_status["oracle_pending"])
                exact_status["finished_vs_synchronous"] = pixel_difference(finished, reference)
                exact_status["validation_ms"] = (time.perf_counter()-validation_started)*1000
            finally:
                projection.backing_lookup, projection.backing_retain = backing_callbacks
                if controller is not None:
                    canvas._scene_controller = controller
            probe.phase = "shutdown"
    write_json("phases.json", probe.rows)
    write_json("inputs.json", probe.input_rows)
    write_json("frames.json", probe.frames)
    write_json("controls.json", probe.controls)
    write_json("heartbeat.json", heartbeat_samples)
    queue_events = [(row["posted_at"], 1) for row in probe.input_rows]
    queue_events.extend((row["dispatched_at"], -1) for row in probe.input_rows if "dispatched_at" in row)
    depth = max_depth = 0
    for _, change in sorted(queue_events):
        depth += change
        max_depth = max(max_depth, depth)
    summary = {
        "native_graphics": native_graphics(),
        "timed_out": timed_out, "posted_inputs": len(probe.input_rows),
        'input_start': probe.input_start,
        'first_native_display_ms': (probe.first_native_paint_at-probe.exposure_requested_at)*1000
            if probe.first_native_paint_at is not None and probe.exposure_requested_at is not None else None,
        'native_source_preparation': {
            'worker_calls': sum(not row['gui_thread'] for row in probe.rows
                                if row['name'] == 'sources.native_input'),
            'gui_calls': sum(row['gui_thread'] for row in probe.rows
                             if row['name'] == 'sources.native_input'),
        },
        "dispatched_inputs": sum("dispatched_at" in r for r in probe.input_rows),
        "maximum_queued_tablet_events": max_depth,
        "remaining_queued_tablet_events": depth,
        "signals": dict(probe.signals), "heartbeat_gaps": distribution(heartbeat_gaps),
        "heartbeat_input": distribution([r["gap_ms"] for r in heartbeat_samples if r["phase"] == "input"]),
        "input": {key: distribution([r[key] for r in probe.input_rows if key in r])
                  for key in ("scheduled_to_dispatch_ms", "posted_to_dispatch_ms", "producer_late_ms")},
        "paint": distribution([r["paint_ms"] for r in probe.frames if r["phase"] == "input"]),
        "cold_paint": distribution([r["paint_ms"] for r in probe.frames if r["phase"] == "cold"]),
        "camera_endpoint_max_error": max((abs(actual-expected)
                                           for row in probe.controls if row.get("expected_camera")
                                           for actual, expected in zip(row["camera"], row["expected_camera"])),
                                          default=0.),
        "input_to_next_paint": distribution([r["oldest_input_to_paint_ms"] for r in probe.frames
                                             if "oldest_input_to_paint_ms" in r]),
        "input_to_prepared_raster_feedback_paint": distribution([
            r["oldest_input_to_paint_ms"] for r in probe.frames
            if "oldest_input_to_paint_ms" in r and r['raster_feedback_tiles']
            and r['current_raster_contact_covered']]),
        "prepared_raster_feedback_frames": sum(bool(r['raster_feedback_tiles']) for r in probe.frames),
        "prepared_current_raster_contact_frames": sum(bool(r['raster_feedback_tiles'])
            and r['current_raster_contact_covered'] for r in probe.frames),
        "first_press_after_navigation": distribution([r["posted_to_dispatch_ms"] for r in probe.input_rows
                                                       if r.get("after_navigation") and r["kind"] == "press"
                                                       and "posted_to_dispatch_ms" in r]),
        "stroke_metrics": [{"stroke": stroke,
                            "queue": distribution([r["posted_to_dispatch_ms"] for r in probe.input_rows
                                                   if r.get("stroke") == stroke and "posted_to_dispatch_ms" in r]),
                            "paint": distribution([r["paint_ms"] for r in probe.frames
                                                    if r["activity"] == f"stroke-{stroke}"])}
                           for stroke in range(args.strokes)],
        "phases": [{"phase": phase, "name": name, **distribution(values)}
                   for (phase, name), values in sorted(probe.samples.items())],
        "autosaves_submitted": window._autosave_jobs.submitted,
        "command_revision": canvas.command_stack.revision,
        "input_history_revision": input_history_revision,
        "history_labels": [command.label for command in canvas.command_stack._undo],
        "projection": measured_projection,
        "async_exact": exact_status,
    }
    if args.scenario == 'occupied':
        selected_effects = {identifier for identifier in chapter.objects[RASTER_ID].modifier_ids
                            if not chapter.modifiers[identifier].muted
                            and (chapter.modifiers[identifier].intensity > 0
                                 or chapter.modifiers[identifier].parameter_masks)}
        evaluated = {effect['id'] for row in probe.rows
                     if row['phase'] in ({'cold'} if args.cold_only else {'input', 'drain', 'validation'})
                     for effect in row.get('modifiers', [])}
        evaluated_held = {effect['id'] for row in probe.rows if row['phase'] == 'input'
                          for effect in row.get('modifiers', [])}
        summary['selected_effects_evaluated'] = sorted(selected_effects & evaluated)
        summary['selected_effects_evaluated_during_input'] = sorted(selected_effects & evaluated_held)
        summary['selected_effects_expected'] = sorted(selected_effects)
    window._dirty = False
    if graphics_before_shutdown is not None:
        summary['graphics_worker'] = graphics_before_shutdown
    if preparation_before_shutdown is not None:
        summary['worker_preparation'] = preparation_before_shutdown
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    gpu = getattr(canvas, "_gpu_pattern_renderer", None)
    if gpu is not None:
        try:
            canvas.destroyed.disconnect(gpu.close)
        except (RuntimeError, TypeError):
            pass
        gpu.close()
    window.close()
    controller = getattr(canvas, '_scene_controller', None)
    if controller is not None:
        controller.reset()
        controller.scheduler.close()
        controller.scheduler.executor.shutdown(wait=True, cancel_futures=False)
    graphics = getattr(canvas, '_graphics_worker', None)
    if graphics is not None:
        graphics.close()
    presenter = getattr(canvas, '_document_tile_presenter', None)
    if presenter is not None:
        presenter.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    import gc
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    after_hashes = manifest()
    write_json("project-files-after.json", after_hashes)
    summary["project_files_unchanged"] = before_hashes == after_hashes
    write_json("summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "phases"}, indent=2), flush=True)
    for owner, name, original in reversed(probe.patches):
        setattr(owner, name, original)
    assert before_hashes == after_hashes, "Source project copy was unexpectedly written"
    assert summary["camera_endpoint_max_error"] < 1e-6, "Scripted tablet path and actual camera diverged"
    if args.scenario == 'occupied':
        assert selected_effects <= evaluated, 'Occupied drawing did not evaluate every active selected effect'
    if not args.cold_only:
        assert summary['command_revision'] == input_history_revision + args.strokes, (
            'Every scripted stroke must create exactly one history transaction', summary['history_labels'])
    if args.async_exact:
        assert not timed_out and exact_status["terminal_ready"], "Exact projection did not finish cleanly"
        assert not exact_status["unpresented_exact_inputs"], "Input never reached a finished exact frame"
        assert exact_status["finished_vs_synchronous"]["identical"], "Async pixels differ from exact oracle"
        assert not exact_status["oracle_pending"]["frame_pending"], "Synchronous oracle did not finish its native frame"
        assert not exact_status["oracle_pending"]["jobs_busy"], "Synchronous oracle deferred effect work"
