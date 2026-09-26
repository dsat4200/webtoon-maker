"""Manual hidden-native replay of mask wand on the copied saved slam image.

No project saves, live UI, MainWindow, or Blender integration. A real mouse
click exercises normal wand dispatch; selection and its following paint are
timed separately. --profile records diagnostic CPU costs, not final timings.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / '.artifacts/mask-wand-20260926'
COPY = ART / 'project-copy'
CHAPTER = '0a72f08009294aa0a3d14e6a38e22bbb'
OBJECT = 'd05523e0b3084570abefb49981eafe2a'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', required=True)
parser.add_argument('--baseline', action='store_true')
parser.add_argument('--source-snapshot', type=Path)
parser.add_argument('--profile', action='store_true')
parser.add_argument('--disconnected', action='store_true')
parser.add_argument('--ignore-other-layers', action='store_true')
parser.add_argument('--repeat', action='store_true', help='Measure a warm same-point reselection before cache-clearing oracle.')
parser.add_argument('--selection-budget', type=float, default=90.)
args = parser.parse_args()
OUT = (ART / args.label).resolve()
assert OUT.is_relative_to(ART.resolve()) and OUT != ART.resolve() and not OUT.exists()
assert COPY.resolve().is_relative_to((ROOT/'.artifacts').resolve())
OUT.mkdir(parents=True)
SOURCE = ART/'baseline-source' if args.baseline else ROOT
if args.source_snapshot:
    SOURCE = args.source_snapshot.resolve()
    assert SOURCE.is_relative_to(ART.resolve()) and (SOURCE/'comic_editor').is_dir()
sys.path.insert(0, str(SOURCE))
os.environ['QT_QPA_PLATFORM'] = 'windows'
os.environ['QT_TLS_BACKEND'] = 'schannel'

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QSurfaceFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from comic_editor.core import settings as settings_module
from comic_editor.core.models import ToneMask, HueSaturationLightnessModifier, ParameterMaskBinding
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui import canvas as canvas_module, distort_rendering
from comic_editor.ui.canvas import create_canvas, ToolKind
from drawing_benchmark_support import read_native_frame, pixel_difference

def write(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2), encoding='utf-8')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def image_hash(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return hashlib.sha256(bytes(image.constBits())).hexdigest()

files = [COPY/'chapters'/CHAPTER/'chapter.json',
         COPY/'chapters'/CHAPTER/'images'/OBJECT/'last-frame.png']
before = {str(p): digest(p) for p in files}
write('input-hashes-before.json', before)
source_hashes = {str(p.relative_to(SOURCE)): digest(p) for p in sorted((SOURCE/'comic_editor').rglob('*.py'))}
write('source-hashes.json', source_hashes)
settings_module.settings_path = lambda: OUT/'settings.json'
fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
fmt.setSamples(0)
fmt.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(fmt)
app = QApplication([])
phase = 'setup'
rows = []
first_paint_end = None
selection_start = None
original_paint = canvas_module._CanvasLogic.paintEvent
def paint(self, event):
    global first_paint_end
    start = time.perf_counter()
    original_paint(self, event)
    end = time.perf_counter()
    if phase != 'setup':
        rows.append({'phase': phase, 'kind': 'paint', 'ms': (end-start)*1000})
        if phase == 'redraw' and first_paint_end is None:
            first_paint_end = end
canvas_module._CanvasLogic.paintEvent = paint
settings = EditorSettings(canvas_renderer='auto', snap_to_grid=False,
                          predictive_ink=False, grid_overlay_visible=False,
                          mask_wand_tolerance=16)
settings.mask_wand_connected = not args.disconnected
settings.mask_wand_ignore_other_layers = args.ignore_other_layers
canvas = create_canvas(settings)
canvas.resize(1000, 950)
canvas.setAttribute(Qt.WA_DontShowOnScreen, True)
canvas.setAttribute(Qt.WA_ShowWithoutActivating, True)
chapter, tiles, images = SeriesRepository(COPY).load_chapter(CHAPTER, include_images=True)
mask = ToneMask(mask_id='f'*32, saved=True, name='Benchmark cyan mask')
chapter.masks[mask.mask_id] = mask
hsl = HueSaturationLightnessModifier(modifier_id='e'*32, hue=30,
    parameter_masks={'intensity': ParameterMaskBinding(mask_id=mask.mask_id)})
chapter.add_modifier(hsl, [('object', OBJECT)])
canvas.set_document(chapter, tiles, images)
canvas.set_selection('object', OBJECT)
canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = 540., 19750., .6, 0.
canvas.set_tone_mask_mode(mask.mask_id)
assert canvas.set_tool(ToolKind.MASK_WAND)
canvas.show()
app.processEvents()
canvas.repaint()
app.processEvents()
initial = read_native_frame(canvas)
initial.save(str(OUT/'before.png'))
preferred = canvas.document_to_widget(QPointF(100, 19600))
rgba = initial.convertToFormat(QImage.Format_RGBA8888)
pixels = np.frombuffer(rgba.constBits(), np.uint8).reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4].reshape(rgba.height(), rgba.width(), 4)
ys, xs = np.where((pixels[...,0] < 120) & (pixels[...,1] > 200) & (pixels[...,2] > 200))
assert len(xs), 'Expected screenshot cyan source region must be visible before timing'
which = np.argmin((xs-preferred.x())**2+(ys-preferred.y())**2)
widget_seed = QPointF(float(xs[which]), float(ys[which])).toPoint()
seed = canvas.widget_to_document(QPointF(widget_seed))
write('fixture.json', {'chapter': CHAPTER, 'object': OBJECT, 'seed': list(seed.toTuple()),
                       'seed_rgba': pixels[ys[which], xs[which]].tolist(),
                       'mesh': chapter.modifiers['2293035805f6457eaaf38d1436adf62e'].to_dict(),
                       'hsl': hsl.to_dict(), 'connected': not args.disconnected,
                       'ignore_other_layers': args.ignore_other_layers,
                       'camera': [canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation],
                       'chapter_size': [chapter.width,chapter.height]})

class BudgetExceeded(RuntimeError):
    pass

original_preview = canvas.render_preview
reference_count = 0
object_calls = Counter()
original_object_render = canvas._render_object
def object_render(painter, obj, *a, **kw):
    if phase == 'selection':
        object_calls[obj.object_id] += 1
    return original_object_render(painter, obj, *a, **kw)
canvas._render_object = object_render
def preview(image, *a, **kw):
    global reference_count
    if phase == 'selection' and time.perf_counter()-selection_start > args.selection_budget:
        raise BudgetExceeded('Selection reference capture exceeded bounded diagnostic budget')
    start = time.perf_counter()
    flags = {'interactive':canvas._interactive_render,
             'projection_exact':getattr(canvas,'_projection_exact',False),
             'bounds_enabled':canvas._render_bounds.enabled,
             'bounds_exact_sampling':canvas._render_bounds.exact_sampling,
             'bounds_usable':canvas._render_bounds.usable()}
    try:
        return original_preview(image, *a, **kw)
    finally:
        if phase == 'selection':
            reference_count += 1
            rect = kw.get('source_rect')
            rows.append({'phase':phase,'kind':'reference','ms':(time.perf_counter()-start)*1000,
                         'rect': list(rect.getRect()) if rect else None, 'flags':flags})
            if reference_count % (128 if args.disconnected else 8) == 0:
                print(json.dumps({'reference_tiles':reference_count,'elapsed_s':time.perf_counter()-selection_start}),flush=True)
canvas.render_preview = preview
original_distort = distort_rendering.render_distort
def distort(*a, **kw):
    start = time.perf_counter()
    try:
        return original_distort(*a, **kw)
    finally:
        if phase != 'setup':
            rows.append({'phase':phase,'kind':'distort','ms':(time.perf_counter()-start)*1000,
                         'modifier': getattr(a[2], 'modifier_id', None) if len(a)>2 else None,
                         'size':[a[0].width(),a[0].height()] if a else None})
distort_rendering.render_distort = distort
original_fill = TileStore.advanced_fill
def fill(self, *a, **kw):
    # Frozen source predates the checkbox. This single profile-bit substitution
    # provides its existing all-matching algorithm as the comparison oracle.
    if args.baseline and args.disconnected:
        a = list(a)
        a[4] = {**a[4], 'connected_pixels_only':False}
    start = time.perf_counter()
    try:
        return original_fill(self, *a, **kw)
    finally:
        rows.append({'phase':phase,'kind':'advanced_fill','ms':(time.perf_counter()-start)*1000})
TileStore.advanced_fill = fill
profile = None
if args.profile:
    import cProfile
    profile = cProfile.Profile()
phase = 'selection'
selection_start = time.perf_counter()
error = None
if profile:
    profile.enable()
try:
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, widget_seed)
except BaseException as exc:
    error = f'{type(exc).__name__}: {exc}'
finally:
    selection_end = time.perf_counter()
    if profile:
        profile.disable()
        profile.dump_stats(str(OUT/'selection.prof'))
        import pstats
        with (OUT/'selection-profile.txt').open('w',encoding='utf-8') as stream:
            pstats.Stats(profile,stream=stream).sort_stats('cumulative').print_stats(60)
phase = 'redraw'
app.processEvents()
if first_paint_end is None:
    canvas.repaint()
final = read_native_frame(canvas)
final.save(str(OUT/'finished.png'))
mask_hashes = {}
for key, tile in canvas.tiles.object_tiles(mask.mask_id).items():
    tile.save(str(OUT/f'mask-{key[0]}-{key[1]}.png'))
    mask_hashes[str(key)] = image_hash(tile)
write('mask-hashes.json', mask_hashes)
reference_ms = sum(r['ms'] for r in rows if r['kind']=='reference')
summary = {'baseline':args.baseline,'profiled':args.profile,'error':error,
    'selection_ms':(selection_end-selection_start)*1000,
    'click_to_first_redraw_ms':(first_paint_end-selection_start)*1000 if first_paint_end else None,
    'reference_tiles':reference_count,'reference_total_ms':reference_ms,
    'non_reference_selection_ms':(selection_end-selection_start)*1000-reference_ms,
    'counts':dict(Counter((r['phase']+'.'+r['kind']) for r in rows)),
    'mask_tiles':len(mask_hashes),'mask_revision':mask.revision,
    'selection_object_render_calls':dict(object_calls),
    'command_revision':canvas.command_stack.revision}
if args.repeat:
    phase = 'repeat_selection'
    undo_before = canvas.command_stack.revision
    repeat_start = time.perf_counter()
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, widget_seed)
    repeat_end = time.perf_counter()
    phase = 'repeat_redraw'
    app.processEvents()
    repeat_painted = time.perf_counter()
    repeated_hashes = {str(key):image_hash(tile) for key,tile in canvas.tiles.object_tiles(mask.mask_id).items()}
    summary['repeat'] = {'selection_ms':(repeat_end-repeat_start)*1000,
                         'click_through_processed_redraw_ms':(repeat_painted-repeat_start)*1000,
                         'mask_hashes_unchanged':repeated_hashes==mask_hashes,
                         'command_revision_unchanged':canvas.command_stack.revision==undo_before}
phase = 'oracle'
canvas._projection_async_enabled = False
canvas._projection_cull_outside_view = False
canvas._effect_jobs.cancel()
canvas._modifier_render_cache.clear(); canvas._modifier_render_cache_bytes = 0
canvas._modifier_source_cache.clear(); canvas._modifier_source_cache_bytes = 0
canvas._distort_preparation_cache = None
canvas._document_projection.clear()
canvas._invalidate_scene_cache()
canvas._invalidate_tone_mask_overlay()
canvas.repaint()
oracle = read_native_frame(canvas)
oracle.save(str(OUT/'oracle.png'))
summary['finished_vs_fresh_oracle'] = pixel_difference(final,oracle)
after = {str(p):digest(p) for p in files}
write('input-hashes-after.json', after)
summary['input_files_unchanged'] = before==after
write('events.json',rows)
write('summary.json',summary)
print(json.dumps({k:v for k,v in summary.items() if k!='selection_object_render_calls'}),flush=True)
canvas._effect_jobs.cancel()
canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
gpu = getattr(canvas,'_gpu_pattern_renderer',None)
if gpu:
    try:
        canvas.destroyed.disconnect(gpu.close)
    except (RuntimeError,TypeError):
        pass
    gpu.close()
canvas.close()
assert before==after and error is None and mask_hashes
assert summary['finished_vs_fresh_oracle']['identical']
if args.repeat:
    assert summary['repeat']['mask_hashes_unchanged'] and summary['repeat']['command_revision_unchanged']
