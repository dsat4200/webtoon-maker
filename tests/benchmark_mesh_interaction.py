"""Manual real-control mesh replay, isolated saved image; never saves a project.

Native hidden Canvas plus actual ModifierControls signals. Producer deadlines
remain fixed while GUI paints; no per-move settling or forced frame delivery.
This isolates one selected mesh and excludes MainWindow ancillary refresh.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / '.artifacts/mesh-warp-20260926'
INPUT = ROOT / '.artifacts/drawing-stutters-20260926/project-copy'
CHAPTER = '0a72f08009294aa0a3d14e6a38e22bbb'
OBJECT = 'd05523e0b3084570abefb49981eafe2a'
MODIFIER = '2293035805f6457eaaf38d1436adf62e'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', required=True)
parser.add_argument('--baseline', action='store_true')
parser.add_argument('--rows', type=int, default=4, choices=(4, 6))
parser.add_argument('--profile-release', action='store_true',
                    help='Profile one exact handle-release paint; this run is not timing evidence.')
args = parser.parse_args()
OUT = (ART / args.label).resolve()
assert OUT.is_relative_to(ART.resolve()) and OUT != ART.resolve() and not OUT.exists()
OUT.mkdir(parents=True)
SOURCE = ART / 'baseline-source' if args.baseline else ROOT
sys.path.insert(0, str(SOURCE))
os.environ['QT_QPA_PLATFORM'] = 'windows'
os.environ['QT_TLS_BACKEND'] = 'schannel'

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QTimer, Qt
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication, QSlider
from comic_editor.core import settings as settings_module
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.ui import canvas as canvas_module, distort_rendering
from comic_editor.ui.canvas import create_canvas
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.distort_controls import DistortControls
from drawing_benchmark_support import canvas_pending, pixel_difference, read_native_frame

def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2), encoding='utf-8')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def stats(values):
    return ({'count': len(values), 'p50_ms': float(np.percentile(values, 50)),
             'p95_ms': float(np.percentile(values, 95)), 'max_ms': max(values)}
            if values else {'count': 0})

files = [INPUT / 'chapters' / CHAPTER / 'chapter.json',
         INPUT / 'chapters' / CHAPTER / 'images' / OBJECT / 'last-frame.png']
before = {str(p): digest(p) for p in files}
source_hashes = {str(p.relative_to(SOURCE)): digest(p)
                 for p in sorted((SOURCE / 'comic_editor').rglob('*.py'))}
save('source-hashes.json', source_hashes)
save('input-hashes-before.json', before)
settings_module.settings_path = lambda: OUT / 'settings.json'
fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
fmt.setSamples(0)
fmt.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(fmt)
app = QApplication([])
original_paint = canvas_module._CanvasLogic.paintEvent
def paint_dispatch(self, event):
    callback = globals().get('paint')
    return callback(self, event) if callback else original_paint(self, event)
canvas_module._CanvasLogic.paintEvent = paint_dispatch
canvas = create_canvas(EditorSettings(canvas_renderer='auto', snap_to_grid=False,
                                      predictive_ink=False, grid_overlay_visible=False))
canvas.resize(1000, 850)
canvas.setAttribute(Qt.WA_DontShowOnScreen, True)
canvas.setAttribute(Qt.WA_ShowWithoutActivating, True)
chapter, tiles, images = SeriesRepository(INPUT).load_chapter(CHAPTER, include_images=True)
canvas.set_document(chapter, tiles, images)
canvas.set_solo_entities({('object', OBJECT)})
canvas.set_selection('object', OBJECT)
canvas.modifier_mode, canvas.active_modifier_id = True, MODIFIER
modifier = chapter.modifiers[MODIFIER]
canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = 540., 19800., .45, 0.
owner = ModifierControls(canvas)
owner.active_modifier_id = MODIFIER
controls = DistortControls(modifier, owner)
if args.rows != modifier.parameters['rows']:
    controls.set_value('rows', args.rows)
slider = controls.findChild(QSlider, 'distortSlider_smoothness')
assert slider is not None
canvas.show()
app.processEvents()
assert canvas.isValid()
canvas.repaint()
app.processEvents()
point = dict(canvas._distort_handle_points(modifier))[5]
initial_point = list(modifier.parameters['destination_points'][5]) if 'destination_points' in modifier.parameters else None
save('fixture.json', {'object': OBJECT, 'modifier': modifier.to_dict(),
                      'camera': [canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation],
                      'handle': [point.x(), point.y()], 'slider_initial': slider.value(),
                      'source_files': len(source_hashes), 'scope': 'solo saved slam, direct real Canvas + ModifierControls'})

frames, effects, inputs, beats, checkpoints = [], [], [], [], []
phase = 'handle'
started = time.perf_counter()
last_input = started
last_sequence = -1
done = False
release_profile = None
preview_profile = None
original_distort = distort_rendering.render_distort

def paint(self, event):
    global release_profile, preview_profile
    start = time.perf_counter()
    capture = bool(args.profile_release and phase != 'oracle' and done and release_profile is None)
    preview_capture = bool(args.profile_release and phase != 'oracle' and not done
                           and last_sequence > 0 and preview_profile is None)
    profiler = None
    if capture or preview_capture:
        import cProfile
        profiler = cProfile.Profile()
        if capture:
            release_profile = profiler
        else:
            preview_profile = profiler
        profiler.enable()
    try:
        original_paint(self, event)
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(OUT/('release.prof' if capture else 'preview.prof')))
    if self is canvas:
        now = time.perf_counter()
        frames.append({'phase': phase, 'ms': (now-start)*1000,
                       'at_ms': (now-started)*1000, 'sequence': last_sequence,
                       'provisional': bool(getattr(self, '_scene_cache_provisional', False)),
                       'mesh_preview': bool(getattr(self, '_mesh_warp_preview_presented', False)),
                       'pending': canvas_pending(self)})

def distort(*a, **kw):
    start = time.perf_counter()
    try:
        return original_distort(*a, **kw)
    finally:
        image = a[0] if a else kw.get('image')
        effects.append({'phase': phase, 'ms': (time.perf_counter()-start)*1000,
                        'size': [image.width(), image.height()] if image is not None else None})

distort_rendering.render_distort = distort
event_type = QEvent.Type(QEvent.registerEventType())
actions = [(0., 'handle_begin', 0)]
actions += [(i/60, 'handle_move', i) for i in range(1, 61)]
actions += [(1.02, 'handle_end', 0), (2.5, 'slider_begin', 0)]
actions += [(2.5+i/60, 'slider_move', i) for i in range(1, 61)]
actions += [(3.52, 'slider_end', 0)]
if args.profile_release:
    actions = actions[:62]

class Input(QEvent):
    def __init__(self, sequence, when, kind, index):
        super().__init__(event_type)
        self.sequence, self.when, self.kind, self.index = sequence, when, kind, index

class Driver(QObject):
    def event(self, event):
        global phase, last_input, last_sequence, done
        if event.type() != event_type:
            return super().event(event)
        start = time.perf_counter()
        phase = event.kind.split('_')[0]
        kind, i = event.kind, event.index
        if kind == 'handle_begin':
            assert canvas._begin_modifier_handle(point)
        elif kind == 'handle_move':
            assert canvas._move_modifier_handle(point + QPointF(25*i/60, 14*math.sin(i/60*math.pi/2)))
        elif kind == 'handle_end':
            assert canvas._finish_modifier_handle()
            if args.profile_release:
                done = True
        elif kind == 'slider_begin':
            slider.setSliderDown(True)
        elif kind == 'slider_move':
            # Smoothness has one decimal place; use real slider range ratio.
            slider.setValue(round(slider.maximum()*(.5 + .15*i/60)))
        elif kind == 'slider_end':
            slider.setSliderDown(False)
            done = True
        last_input, last_sequence = time.perf_counter(), event.sequence
        inputs.append({'kind': kind, 'sequence': event.sequence,
                       'queue_ms': (start-event.when)*1000, 'handler_ms': (last_input-start)*1000,
                       'at_ms': (start-started)*1000})
        return True

driver = Driver()
def producer():
    for seq, (offset, kind, index) in enumerate(actions):
        deadline = started+offset
        time.sleep(max(0, deadline-time.perf_counter()))
        QCoreApplication.postEvent(driver, Input(seq, deadline, kind, index))
thread = threading.Thread(target=producer, daemon=True)
last_beat = started
timeout = False

def tick():
    global last_beat, timeout
    now = time.perf_counter()
    beats.append((now-last_beat)*1000)
    last_beat = now
    pending = canvas_pending(canvas)
    if now-started > 60:
        timeout = True
        app.quit()
    elif done and now-last_input > .25 and not pending['jobs_busy'] and not pending['frame_pending']:
        app.quit()

timer = QTimer()
timer.setInterval(20)
timer.timeout.connect(tick)
timer.start()
thread.start()
app.exec()
timer.stop()
thread.join(timeout=1)
finished = read_native_frame(canvas)
finished.save(str(OUT/'finished.png'))
terminal = canvas_pending(canvas)
save('events.json', {'inputs': inputs, 'frames': frames, 'effects': effects, 'heartbeat_ms': beats})
summary = {'baseline': args.baseline, 'rows': args.rows, 'timeout': timeout, 'actions': len(inputs), 'expected_actions': len(actions),
           'queue': stats([r['queue_ms'] for r in inputs]), 'heartbeat': stats(beats),
           'frames': {}, 'effects': {}, 'terminal': terminal,
           'final_smoothness': modifier.parameters['smoothness'],
           'final_mesh_preview': bool(getattr(canvas, '_mesh_warp_preview_presented', False)),
           'command_revision': canvas.command_stack.revision}
for name in ('handle', 'slider'):
    selected = [r for r in frames if r['phase'] == name]
    summary['frames'][name] = {**stats([r['ms'] for r in selected]),
                              'mesh_preview_frames': sum(r['mesh_preview'] for r in selected),
                              'preview_paint': stats([r['ms'] for r in selected if r['mesh_preview']]),
                              'finished_paint': stats([r['ms'] for r in selected if not r['mesh_preview']]),
                              'distinct_input_sequences_painted': len(set(r['sequence'] for r in selected))}
    summary['frames'][name]['queue'] = stats([r['queue_ms'] for r in inputs if r['kind'].startswith(name)])
    summary['effects'][name] = {**stats([r['ms'] for r in effects if r['phase'] == name]),
                               'sizes': dict(Counter(str(r['size']) for r in effects if r['phase'] == name))}
phase = 'oracle'
canvas._projection_async_enabled = False
canvas._projection_cull_outside_view = False
canvas._effect_jobs.cancel()
canvas._modifier_render_cache.clear()
canvas._modifier_render_cache_bytes = 0
canvas._modifier_source_cache.clear()
canvas._modifier_source_cache_bytes = 0
canvas._distort_preparation_cache = None
canvas._document_projection.clear()
canvas._invalidate_scene_cache()
canvas._ensure_scene_cache()
canvas.repaint()
oracle = read_native_frame(canvas)
oracle.save(str(OUT/'oracle.png'))
summary['finished_vs_sync_oracle'] = pixel_difference(finished, oracle)
after = {str(p): digest(p) for p in files}
save('input-hashes-after.json', after)
summary['input_files_unchanged'] = before == after
save('summary.json', summary)
if release_profile is not None:
    import pstats
    with (OUT/'release-profile.txt').open('w', encoding='utf-8') as stream:
        pstats.Stats(release_profile, stream=stream).sort_stats('cumulative').print_stats(55)
if preview_profile is not None:
    import pstats
    with (OUT/'preview-profile.txt').open('w', encoding='utf-8') as stream:
        pstats.Stats(preview_profile, stream=stream).sort_stats('cumulative').print_stats(55)
print(json.dumps(summary), flush=True)
canvas._effect_jobs.cancel()
canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
gpu = getattr(canvas, '_gpu_pattern_renderer', None)
if gpu:
    try:
        canvas.destroyed.disconnect(gpu.close)
    except (RuntimeError, TypeError):
        pass
    gpu.close()
canvas.close()
assert not timeout and len(inputs) == len(actions) and before == after
assert not summary['final_mesh_preview'] and summary['finished_vs_sync_oracle']['identical']
