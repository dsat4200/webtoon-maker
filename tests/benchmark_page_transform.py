"""Manual saved-page translation reproduction; copy only, no Blender or saves.

Exercises the native canvas transform tool used by Ctrl+T. Captures the actual
preview and commit at the same translated camera; an overlay-free synchronous
scene render is the strict oracle. HSL mask fields remain as saved.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT/'.artifacts/page-transform-20260926'
COPY = ART/'project-copy'
CHAPTER = '0a72f08009294aa0a3d14e6a38e22bbb'
PAGE = 'bb79f002253b44f8a194a7bbf3a4f348'
IMAGE = '0a688546290744858bf3626dd04ceccd'
RASTER = '9db9d8094e744ee4aaffb84e9db10f7c'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', required=True)
parser.add_argument('--baseline', action='store_true')
parser.add_argument('--mute-hsl', action='store_true')
parser.add_argument('--full-scene', action='store_true')
parser.add_argument('--default-native', action='store_true')
parser.add_argument('--roundtrip', action='store_true')
parser.add_argument('--diagnose-clip', action='store_true')
args = parser.parse_args()
OUT = (ART/args.label).resolve()
assert OUT.is_relative_to(ART.resolve()) and OUT != ART.resolve() and not OUT.exists()
OUT.mkdir(parents=True)
SOURCE = ART/'baseline-source' if args.baseline else ROOT
sys.path.insert(0,str(SOURCE))
os.environ['QT_QPA_PLATFORM']='windows'
os.environ['QT_TLS_BACKEND']='schannel'

import numpy as np
from PySide6.QtCore import QPointF,QRectF,Qt
from PySide6.QtGui import QImage,QSurfaceFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from comic_editor.core import settings as settings_module
from comic_editor.core.settings import EditorSettings
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.models import ChapterDocument
from comic_editor.ui import canvas as canvas_module
from comic_editor.ui.canvas import create_canvas,ToolKind
from drawing_benchmark_support import read_native_frame,pixel_difference,canvas_pending

def save(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2),encoding='utf-8')
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def model_digest():
    return hashlib.sha256(json.dumps(canvas.chapter.to_dict(),sort_keys=True).encode()).hexdigest()
def difference(a,b):
    result=pixel_difference(a,b)
    x=a.convertToFormat(QImage.Format_RGBA8888)
    y=b.convertToFormat(QImage.Format_RGBA8888)
    xa=np.frombuffer(x.constBits(),np.uint8).reshape(x.height(),x.bytesPerLine())[:,:x.width()*4].reshape(x.height(),x.width(),4)
    ya=np.frombuffer(y.constBits(),np.uint8).reshape(y.height(),y.bytesPerLine())[:,:y.width()*4].reshape(y.height(),y.width(),4)
    mask=np.any(xa!=ya,axis=2)
    ys,xs=np.where(mask)
    result['changed_pixels']=int(mask.sum())
    result['changed_pixel_bbox']=[int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())] if len(xs)else None
    return result

settings_module.settings_path=lambda:OUT/'settings.json'
canvas_module.create_network_manager=lambda *_:None
source_hashes={str(p.relative_to(SOURCE)):digest(p)for p in sorted((SOURCE/'comic_editor').rglob('*.py'))}
save('source-hashes.json',source_hashes)
input_paths=[COPY/'chapters'/CHAPTER/'chapter.json',COPY/'chapters'/CHAPTER/'images'/IMAGE/'last-frame.png']
input_before={str(p):digest(p)for p in input_paths}
fmt=QSurfaceFormat();fmt.setVersion(3,3);fmt.setProfile(QSurfaceFormat.CoreProfile);fmt.setSamples(0);fmt.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(fmt)
app=QApplication([])
canvas=create_canvas(EditorSettings(canvas_renderer='auto',snap_to_grid=False,predictive_ink=False,grid_overlay_visible=False))
canvas.resize(1100,1050)
canvas.setAttribute(Qt.WA_DontShowOnScreen,True)
canvas.setAttribute(Qt.WA_ShowWithoutActivating,True)
if not args.default_native:
    canvas._projection_async_enabled=False
chapter,tiles,images=SeriesRepository(COPY).load_chapter(CHAPTER,include_images=True)
assert chapter.layers[PAGE].name=='swage' and chapter.objects[IMAGE].visible
assert chapter.objects[RASTER].visible
if args.mute_hsl:
    for mid in chapter.objects[IMAGE].modifier_ids:
        if chapter.modifiers[mid].modifier_type=='hsl':
            chapter.modifiers[mid].muted=True
canvas.set_document(chapter,tiles,images)
if not args.full_scene:
    canvas.set_solo_entities({('layer',PAGE)})
canvas.set_selection('layer',PAGE)
canvas.set_tool(ToolKind.TRANSFORM)
canvas.center_x,canvas.center_y,canvas.scale,canvas.rotation=548.,19795.,.5,0.
canvas.show();app.processEvents()
save('fixture.json',{'chapter':CHAPTER,'page':PAGE,'image':chapter.objects[IMAGE].to_dict(),
                     'raster':chapter.objects[RASTER].to_dict(),
                     'modifiers':[chapter.modifiers[i].to_dict()for i in chapter.objects[IMAGE].modifier_ids],
                     'delta':[80,120],'camera':[548,19795,.5,0],'mute_hsl':args.mute_hsl,
                     'scope':('full chapter' if args.full_scene else 'solo saved page')+'; actual transform-tool dispatch; native and overlay-free exact renders',
                     'default_native':args.default_native})
captures={};native={};states={};timings={}

def paint_hashes(store,model):
    return {owner:{str(key):hashlib.sha256(bytes(img.constBits())).hexdigest()
                   for key,img in store.iter_tiles(owner)}
            for owner in [RASTER,*model.masks]}

paint_before=paint_hashes(tiles,chapter)
def capture(name,offset=(0,0)):
    start=time.perf_counter()
    canvas.center_x,canvas.center_y=548.+offset[0],19795.+offset[1]
    canvas.scale,canvas.rotation=.5,0.
    canvas.update();canvas.repaint();app.processEvents()
    first_native=read_native_frame(canvas)
    first_native.save(str(OUT/f'{name}-first-native.png'))
    first_pending=canvas_pending(canvas)
    deadline=time.perf_counter()+60
    while args.default_native:
        pending=canvas_pending(canvas)
        assert not pending['failed'],pending
        if not (pending['frame_pending'] or pending['jobs_busy']):
            break
        assert time.perf_counter()<deadline,pending
        app.processEvents();QTest.qWait(10)
    native[name]=read_native_frame(canvas)
    native[name].save(str(OUT/f'{name}-native.png'))
    image=QImage(canvas.width(),canvas.height(),QImage.Format_ARGB32_Premultiplied)
    old=canvas._interactive_render
    canvas._interactive_render=False
    try:
        canvas.render_preview(image,source_rect=QRectF(canvas.center_x-canvas.width()/2/canvas.scale,
            canvas.center_y-canvas.height()/2/canvas.scale,canvas.width()/canvas.scale,canvas.height()/canvas.scale))
    finally:
        canvas._interactive_render=old
    image.save(str(OUT/f'{name}-exact.png'))
    captures[name]=image
    states[name]={'model':model_digest(),'revision':canvas.command_stack.revision,
                  'preview_quad':canvas._transform_preview_quad,'target':canvas._geometry_transform_target,
                  'page':canvas.chapter.layers[PAGE].to_dict(),
                  'warp':canvas.chapter.modifiers[canvas.chapter.objects[IMAGE].modifier_ids[0]].to_dict(),
                  'first_native_pending':first_pending,'settled_pending':canvas_pending(canvas),
                  'first_vs_settled_native':difference(first_native,native[name])}
    save(f'{name}-model.json',canvas.chapter.to_dict())
    timings[name]=(time.perf_counter()-start)*1000
    print(json.dumps({'captured':name,'ms':timings[name]}),flush=True)

delta=QPointF(80,120)
start_world=QPointF(850,20200)
capture('before')
original=model_digest()
def begin_move():
    start=canvas.document_to_widget(start_world).toPoint()
    end=canvas.document_to_widget(start_world+delta).toPoint()
    QTest.mousePress(canvas,Qt.LeftButton,Qt.NoModifier,start)
    assert canvas._geometry_transform_target==('layer_group',PAGE)
    assert canvas._transform_drag_mode=='translate'
    QTest.mouseMove(canvas,end)
    assert canvas._transform_preview_quad is not None
    return end

end=begin_move()
capture('preview',(80,120))
if args.diagnose_clip:
    capture('preview-repeat',(80,120))
    canvas._invalidate_scene_cache()
    capture('preview-invalidate',(80,120))
    canvas._render_bounds._transform=canvas.layer_world_transform
    canvas._invalidate_scene_cache()
    capture('preview-fresh-transform',(80,120))
    save('diagnostic.json',{key:difference(native['preview'],native[key]) for key in
                          ('preview-repeat','preview-invalidate','preview-fresh-transform')})
    canvas._effect_jobs.cancel();canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
    gpu=getattr(canvas,'_gpu_pattern_renderer',None)
    if gpu:
        try:canvas.destroyed.disconnect(gpu.close)
        except(RuntimeError,TypeError):pass
        gpu.close()
    canvas.close()
    sys.exit(0)
preview_model_unchanged=model_digest()==original
QTest.mouseRelease(canvas,Qt.LeftButton,Qt.NoModifier,canvas.document_to_widget(start_world+delta).toPoint())
assert canvas._transform_preview_quad is None
capture('commit',(80,120))
committed=model_digest()
committed_dict=copy.deepcopy(canvas.chapter.to_dict())
canvas.command_stack.undo()
capture('undo')
undo_model_restored=model_digest()==original
canvas.command_stack.redo()
capture('redo',(80,120))
redo_model_restored=model_digest()==committed
canvas.command_stack.undo()
canvas.center_x,canvas.center_y=548.,19795.
end=begin_move()
capture('cancel-preview',(80,120))
QTest.keyClick(canvas,Qt.Key_Escape)
QTest.mouseRelease(canvas,Qt.LeftButton,Qt.NoModifier,canvas.document_to_widget(start_world+delta).toPoint())
capture('cancel')
cancel_model_restored=model_digest()==original
roundtrip={}
if args.roundtrip:
    restored=ChapterDocument.from_dict(copy.deepcopy(committed_dict))
    roundtrip['deserialize_model_exact']=restored.to_dict()==committed_dict
    restored.validate()
    roundtrip['validate_model_exact']=restored.to_dict()==committed_dict
    repository=SeriesRepository(OUT/'saved-roundtrip')
    repository.save_chapter(restored,tiles,images)
    loaded,loaded_tiles,loaded_images=repository.load_chapter(CHAPTER,include_images=True)
    roundtrip['saved_model_exact']=loaded.to_dict()==committed_dict
    roundtrip['mask_and_outline_paint_exact']=paint_hashes(loaded_tiles,loaded)==paint_before
    canvas.set_document(loaded,loaded_tiles,loaded_images)
    if not args.full_scene:
        canvas.set_solo_entities({('layer',PAGE)})
    canvas.set_selection('layer',PAGE)
    canvas.set_tool(ToolKind.TRANSFORM)
    capture('roundtrip',(80,120))
    roundtrip['commit_vs_roundtrip_exact']=difference(captures['commit'],captures['roundtrip'])
result={'baseline':args.baseline,'mute_hsl':args.mute_hsl,
        'full_scene':args.full_scene,'default_native':args.default_native,'roundtrip':roundtrip,
        'preview_model_unchanged':preview_model_unchanged,'undo_model_restored':undo_model_restored,
        'redo_model_restored':redo_model_restored,'cancel_model_restored':cancel_model_restored,
        'mask_and_outline_paint_unchanged':paint_hashes(tiles,chapter)==paint_before,
        'exact':{},'native':{},'capture_ms':timings}
for name,a,b in [('preview_vs_commit','preview','commit'),('before_vs_undo','before','undo'),
                 ('commit_vs_redo','commit','redo'),('before_vs_cancel','before','cancel'),
                 ('preview_vs_cancel_preview','preview','cancel-preview'),('before_vs_shifted_commit','before','commit')]:
    result['exact'][name]=difference(captures[a],captures[b])
    result['native'][name]=difference(native[a],native[b])
save('states.json',states)
result['input_files_unchanged']=input_before=={str(p):digest(p)for p in input_paths}
save('summary.json',result)
print(json.dumps(result),flush=True)
canvas._effect_jobs.cancel();canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
gpu=getattr(canvas,'_gpu_pattern_renderer',None)
if gpu:
    try:canvas.destroyed.disconnect(gpu.close)
    except(RuntimeError,TypeError):pass
    gpu.close()
canvas.close()
assert result['input_files_unchanged'] and preview_model_unchanged and undo_model_restored and redo_model_restored
assert cancel_model_restored and result['mask_and_outline_paint_unchanged']
if not args.baseline:
    for key in ('preview_vs_commit','before_vs_undo','commit_vs_redo','before_vs_cancel'):
        assert result['exact'][key]['identical'],(key,result['exact'][key])
    for key in ('preview_vs_commit','before_vs_undo','commit_vs_redo'):
        assert result['native'][key]['identical'],(key,result['native'][key])
if args.roundtrip:
    assert all(roundtrip[key]for key in ('deserialize_model_exact','validate_model_exact','saved_model_exact','mask_and_outline_paint_exact'))
    assert roundtrip['commit_vs_roundtrip_exact']['identical']
