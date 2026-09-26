"""Isolated native Smudge window: sustained input, exact release and UI evidence.

Creates only a synthetic project below .artifacts. No MainWindow, Blender,
network, original project, or user settings access. Input deadlines do not wait
for each paint; preview and settled exact frames are counted separately.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', default='native')
parser.add_argument('--visual-only', action='store_true')
args = parser.parse_args()
ART = ROOT / '.artifacts/smudge-20260927'
OUT = (ART / args.label).resolve()
assert OUT.is_relative_to(ART.resolve()) and not OUT.exists()
OUT.mkdir(parents=True)
os.environ['QT_QPA_PLATFORM'] = 'windows'
os.environ['QT_TLS_BACKEND'] = 'schannel'

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPointingDevice, QSurfaceFormat, QTabletEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QScrollArea, QWidget
from comic_editor.core import settings as settings_module
from comic_editor.core.models import BoundGeometry, ChapterDocument, ChapterReference, DistortModifier, RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.smudge import default_tool_settings, fit_smudge_stroke
from comic_editor.core.tiles import TileStore
from comic_editor.ui import canvas as canvas_module, smudge_rendering
from comic_editor.ui.canvas import create_canvas, ToolKind
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_presets import ModifierPresetController
from comic_editor.ui.smudge_controls import SmudgeControls
from drawing_benchmark_support import canvas_pending, pixel_difference, read_native_frame


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2), encoding='utf-8')


def source_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / 'comic_editor').rglob('*.py'))}


def stats(values):
    return ({'count': len(values), 'median_ms': float(np.percentile(values, 50)),
             'p95_ms': float(np.percentile(values, 95)), 'max_ms': float(max(values))}
            if values else {'count': 0})


settings_module.settings_path = lambda: OUT / 'settings.json'
canvas_module.create_network_manager = lambda *_: None
before_source = source_hashes()
save('source-hashes.json', before_source)
fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
fmt.setSamples(0)
fmt.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(fmt)
app = QApplication([])
app.setStyleSheet((ROOT / 'comic_editor/ui/style.qss').read_text(encoding='utf-8'))
paint_original = canvas_module._CanvasLogic.paintEvent
frames, inputs, effects, beats = [], [], [], []
phase, sequence = 'warmup', -1


def painted(self, event):
    start = time.perf_counter()
    paint_original(self, event)
    if self is globals().get('canvas'):
        frames.append({'phase': phase, 'sequence': sequence,
                       'at': time.perf_counter(), 'ms': (time.perf_counter() - start) * 1000,
                       'preview': bool(getattr(self, '_mesh_warp_preview_presented', False)),
                       'pending': canvas_pending(self)})


canvas_module._CanvasLogic.paintEvent = painted
render_original = smudge_rendering.render_smudge


def rendered(*a, **kw):
    start = time.perf_counter()
    try:
        return render_original(*a, **kw)
    finally:
        effects.append({'phase': phase, 'ms': (time.perf_counter() - start) * 1000,
                        'source': [a[0].width(), a[0].height()]})


smudge_rendering.render_smudge = rendered
window = QWidget()
window.setWindowTitle('Smudge — isolated native verification')
window.setAttribute(Qt.WA_DontShowOnScreen, True)
window.setAttribute(Qt.WA_ShowWithoutActivating, True)
layout = QHBoxLayout(window)
canvas = create_canvas(EditorSettings(canvas_renderer='auto', snap_to_grid=False,
                                      predictive_ink=False, grid_overlay_visible=False))
canvas.setFixedSize(1000, 1000)
layout.addWidget(canvas)
chapter = ChapterDocument(name='Smudge color study', width=1024, height=1024, document_kind='image')
page = chapter.add_page('Color study', BoundGeometry.rectangle(0, 0, 1024, 1024))
page.fill_color, page.border_width = None, 0
obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 1024, 1024)))
source = QImage(1024, 1024, QImage.Format_ARGB32_Premultiplied)
source.fill(QColor('#eeeeee'))
painter = QPainter(source)
for index, color in enumerate(('#f54141', '#e59d29', '#dcc535', '#43ad68', '#388bcc', '#7560c4')):
    painter.fillRect(80 + index * 144, 70, 120, 885, QColor(color))
painter.end()
source.save(str(OUT / 'source.png'))
tiles = TileStore()
for y in range(4):
    for x in range(4):
        tiles.set_tile(obj.object_id, (x, y), source.copy(x * 256, y * 256, 256, 256))
tool = default_tool_settings()
tool.update(radius=64, flow=72, strength=68, pressure_enabled=False)
strokes = [fit_smudge_stroke([(150, y, 1), (330, y - 70, 1), (600, y + 85, 1), (840, y, 1)], tool)
           for y in (240, 500, 770)]
modifier = DistortModifier(modifier_type='distort_smudge', name='Smudge', frame=(0, 0, 1024, 1024),
                          parameters={'strokes': strokes, 'tool_settings': tool})
chapter.add_modifier(modifier, [('object', obj.object_id)])
canvas.set_document(chapter, tiles)
canvas.set_selection('object', obj.object_id, activate_default_tool=False)
canvas.set_tool(ToolKind.OBJECT_SELECT)
canvas.modifier_mode, canvas.active_modifier_id = True, modifier.modifier_id
canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = 512., 512., .85, 0.
canvas.smudge_select(strokes[-1]['id'], 1)
owner = ModifierControls(canvas)
owner.active_modifier_id = modifier.modifier_id
repository = SeriesRepository(OUT / 'project')
series = repository.create('Isolated smudge study')
series.chapters.append(ChapterReference(chapter.chapter_id, chapter.name))
repository.save_series(series)
ModifierPresetController(owner, lambda: (series, repository))
owner.refresh()
assert canvas._active_smudge_modifier() is modifier
save('model-before.json', chapter.to_dict())
scroll = QScrollArea(window)
scroll.setWidgetResizable(True)
scroll.setFixedWidth(340)
scroll.setWidget(owner)
layout.addWidget(scroll)
window.show()
app.processEvents()
assert canvas.isValid()
canvas.repaint()


def settle(timeout=60):
    deadline = time.perf_counter() + timeout
    while True:
        app.processEvents()
        state = canvas_pending(canvas)
        assert not state['failed'], state
        if not state['frame_pending'] and not state['jobs_busy']:
            return state
        assert time.perf_counter() < deadline, state
        time.sleep(.005)


def native_frame():
    frame = read_native_frame(canvas)
    pixels = np.frombuffer(frame.constBits(), np.uint8).reshape(frame.height(), frame.bytesPerLine())
    assert np.count_nonzero(pixels[:, :frame.width()*4]) > frame.width()*frame.height()*2, 'Blank framebuffer'
    assert len(np.unique(pixels[:, :frame.width()*4:4])) > 32, 'Missing color study'
    return frame


def window_capture(name):
    # QWidget.grab cannot include this hidden OpenGL child and clears its FBO.
    # Capture it first and place those exact native pixels into the live UI grab.
    canvas.repaint()
    settle()
    frame = native_frame()
    frame.save(str(OUT / f'{name}-canvas.png'))
    full = window.grab().toImage()
    painter = QPainter(full)
    painter.drawImage(canvas.mapTo(window, canvas.rect().topLeft()), frame)
    painter.end()
    full.save(str(OUT / f'{name}.png'))
    canvas.repaint()
    settle()


def pointer(kind, position, pressure=1., stylus=False):
    global_position = QPointF(canvas.mapToGlobal(position.toPoint()))
    button = Qt.NoButton if kind == 'move' else Qt.LeftButton
    buttons = Qt.NoButton if kind == 'release' else Qt.LeftButton
    if stylus:
        event = QTabletEvent({'press': QEvent.TabletPress, 'move': QEvent.TabletMove,
                             'release': QEvent.TabletRelease}[kind], QPointingDevice.primaryPointingDevice(),
                            position, global_position, pressure if kind != 'release' else 0.,
                            0., 0., 0., 0., 0., Qt.NoModifier, button, buttons)
    else:
        event = QMouseEvent({'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove,
                            'release': QEvent.MouseButtonRelease}[kind], position, global_position,
                           button, buttons, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, event)


settle()
window_capture('window-before')
native_frame().save(str(OUT / 'before.png'))
actions = []
for index, name in enumerate(('first', 'last', 'flow', 'navigation')):
    offset = index * 3.
    actions.append((offset, name, 'begin', 0))
    actions.extend((offset + i / 60., name, 'move', i) for i in range(1, 61))
    actions.append((offset + 1.02, name, 'end', 60))
if args.visual_only:
    actions = [(0., 'navigation', 'begin', 0), (.02, 'navigation', 'end', 60)]
event_type = QEvent.Type(QEvent.registerEventType())
started = time.perf_counter()
last_input, last_beat = started, started
done, timed_out = False, False
anchor = QPointF()


class Input(QEvent):
    def __init__(self, seq, deadline, name, kind, index):
        super().__init__(event_type)
        self.sequence, self.deadline, self.name, self.kind, self.index = seq, deadline, name, kind, index


class Driver(QObject):
    def event(self, event):
        global phase, sequence, anchor, last_input, done
        if event.type() != event_type:
            return super().event(event)
        start = time.perf_counter()
        phase, sequence = event.name, event.sequence
        if phase == 'navigation':
            if event.kind == 'begin':
                canvas._rebase_touch_navigation([QPointF(500, 500)])
            else:
                displacement = 60 * math.sin(event.index / 60 * 2 * math.pi)
                canvas._apply_touch_navigation([QPointF(500 + displacement, 500)])
                done = event.kind == 'end'
        else:
            if event.kind == 'begin':
                index = 0 if phase == 'first' else -1
                stroke = modifier.parameters['strokes'][index]
                canvas.smudge_select(stroke['id'], 1)
                anchor = (canvas._smudge_gizmo_rects()['flow'].center() if phase == 'flow'
                          else canvas.document_to_widget(QPointF(*stroke['points'][1]['handle'])))
                pointer('press', anchor)
                assert canvas._modifier_handle_drag is not None
            else:
                position = anchor + (QPointF(0, -35 * event.index / 60) if phase == 'flow'
                                     else QPointF(38 * event.index / 60, -24 * event.index / 60))
                pointer('release' if event.kind == 'end' else 'move', position)
        last_input = time.perf_counter()
        inputs.append({'phase': phase, 'kind': event.kind, 'sequence': sequence,
                       'at': start, 'queue_ms': (start - event.deadline) * 1000,
                       'handler_ms': (last_input - start) * 1000})
        return True


driver = Driver()


def producer():
    for sequence, (offset, name, kind, index) in enumerate(actions):
        deadline = started + offset
        time.sleep(max(0, deadline - time.perf_counter()))
        QCoreApplication.postEvent(driver, Input(sequence, deadline, name, kind, index))


def tick():
    global last_beat, timed_out
    now = time.perf_counter()
    beats.append((now - last_beat) * 1000)
    last_beat = now
    state = canvas_pending(canvas)
    if now - started > 60 or state['failed']:
        timed_out = True
        app.quit()
    elif done and now - last_input > .3 and not state['frame_pending'] and not state['jobs_busy']:
        app.quit()


timer = QTimer()
timer.setInterval(20)
timer.timeout.connect(tick)
timer.start()
thread = threading.Thread(target=producer, daemon=True)
thread.start()
app.exec()
timer.stop()
thread.join(1)
finished = native_frame()
finished.save(str(OUT / 'finished.png'))
terminal = canvas_pending(canvas)
save('events.json', {'inputs': inputs, 'frames': frames, 'effects': effects, 'heartbeat_ms': beats})
summary = {'actions': len(inputs), 'expected_actions': len(actions), 'timeout': timed_out,
           'visual_only': args.visual_only,
           'queue': stats([item['queue_ms'] for item in inputs]), 'heartbeat': stats(beats),
           'terminal': terminal, 'phases': {}, 'source_size': [1024, 1024]}
for name in ('first', 'last', 'flow', 'navigation'):
    if not any(item['phase'] == name for item in inputs):
        continue
    paints = [item for item in frames if item['phase'] == name]
    release = next(item['at'] + item['handler_ms'] / 1000 for item in inputs
                   if item['phase'] == name and item['kind'] == 'end')
    ready = next((item for item in paints if item['at'] >= release and not item['preview']
                  and not item['pending']['frame_pending'] and not item['pending']['jobs_busy']), None)
    summary['phases'][name] = {'preview_paint': stats([item['ms'] for item in paints if item['preview']]),
        'exact_paint': stats([item['ms'] for item in paints if not item['preview']]),
        'queue': stats([item['queue_ms'] for item in inputs if item['phase'] == name]),
        'handler': stats([item['handler_ms'] for item in inputs if item['phase'] == name]),
        'release_to_exact_ms': (ready['at'] - release) * 1000 if ready else None,
        'effect_calls': sum(item['phase'] == name for item in effects),
        'painted_input_sequences': len(set(item['sequence'] for item in paints))}

# A fresh exact rebuild must match the displayed finished frame byte for byte.
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
canvas.repaint()
settle()
oracle = native_frame()
oracle.save(str(OUT / 'oracle.png'))
summary['finished_vs_fresh_exact'] = pixel_difference(finished, oracle)

# Exercise real pen events with a nonconstant pressure trace, outside timings.
phase = 'pen'
tool = copy.deepcopy(modifier.parameters['tool_settings'])
tool.update(pressure_enabled=True, pressure_radius=True, pressure_flow=True, pressure_strength=True)
controls = owner.findChild(SmudgeControls)
controls.pressure.setChecked(True)
for check in controls.pressure_channels.values():
    check.setChecked(True)
initial_count = len(modifier.parameters['strokes'])
pointer('press', canvas.document_to_widget(QPointF(130, 400)), .2, True)
pointer('move', canvas.document_to_widget(QPointF(230, 390)), .5, True)
pointer('move', canvas.document_to_widget(QPointF(350, 410)), .9, True)
pointer('release', canvas.document_to_widget(QPointF(450, 430)), 0., True)
settle()
summary['pen_stroke_created'] = len(modifier.parameters['strokes']) == initial_count + 1
summary['pen_pressure'] = modifier.parameters['strokes'][-1]['pressure']
controls.opacity.value.setValue(50)
canvas.repaint()
settle()
native_frame().save(str(OUT / 'opacity-50.png'))
window_capture('window-opacity-50')
strokes_before = copy.deepcopy(modifier.parameters['strokes'])
assert controls.presets.save('Soft pressure brush')
saved_settings = copy.deepcopy(modifier.parameters['tool_settings'])
controls.set_tool_value('radius', 17)
assert controls.presets.load(series.smudge_tool_presets[0]['id'])
repository.save_chapter(chapter, tiles)
loaded_chapter, _ = repository.load_chapter(chapter.chapter_id)
loaded_series = repository.load_series()
summary['preset_tool_only'] = modifier.parameters['strokes'] == strokes_before
summary['preset_roundtrip'] = loaded_series.smudge_tool_presets[0]['settings'] == saved_settings
summary['chapter_roundtrip'] = loaded_chapter.to_dict() == chapter.to_dict()
window_capture('window-preset-loaded')
summary['source_stable'] = before_source == source_hashes()
save('model.json', chapter.to_dict())
save('summary.json', summary)
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
window.close()
assert not timed_out and len(inputs) == len(actions)
assert summary['finished_vs_fresh_exact']['identical'], summary['finished_vs_fresh_exact']
assert all(summary[key] for key in ('pen_stroke_created', 'preset_tool_only', 'preset_roundtrip', 'chapter_roundtrip', 'source_stable'))
assert len({pressure for _, pressure in summary['pen_pressure']}) > 1
