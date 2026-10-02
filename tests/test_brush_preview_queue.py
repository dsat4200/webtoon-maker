"""Preview work yields to input, drops stale requests and preserves pixels."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushTip
from comic_editor.core.brush_preview import (fit_brush_for_preview, iter_brush_preview,
    prepare_blender_preview, preview_samples, render_brush_preview)
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui import brush_preview_queue as queue_module
from comic_editor.ui.brush_controls import BrushPresetCombo, BrushPreview


def reference_preview(brush,color='red'):
    width,height=100,44
    brush=fit_brush_for_preview(brush,height)
    tiles=TileStore();foreground=QColor(color)
    if foreground.alpha()==0:
        tiles.paint_segment('preview',QPointF(0,height/2),QPointF(width,height/2),
                            height*.72,height*.72,QColor('#8daac2'))
        brush=replace(brush,blending_mode='erase',mixing_mode='none',sub_color=(*brush.sub_color[:3],0))
        foreground=QColor('black')
    else:
        prepare_blender_preview(tiles,'preview',brush,width,height)
    stroke=RasterBrushStroke(tiles,'preview',brush,foreground,{},seed=42)
    samples=preview_samples(width,height);stroke.begin(samples[0])
    for sample in samples[1:]:stroke.add(sample)
    stroke.finish()
    image=QImage(width,height,QImage.Format_ARGB32_Premultiplied);image.fill(QColor('#b8b8b8'))
    painter=QPainter(image);painter.fillRect(width//2,0,width-width//2,height,QColor('#4e4e4e'))
    for key,tile in tiles._tiles.get('preview',{}).items():
        painter.drawImage(QPointF(key[0]*tiles.tile_size,key[1]*tiles.tile_size),tile)
    painter.end();return image


@pytest.mark.parametrize('brush,color',[
    (BrushDefinition(size=12,spacing=.15,density=.4),'red'),
    (BrushDefinition(size=12,mixing_mode='running',paint_amount=0,paint_density=0),'blue'),
    (BrushDefinition(size=12,post_correction=.5,stabilization=.4),'red'),
    (BrushDefinition(size=12,taper_end=30,stabilization=.3,
                     dual=BrushDefinition(size=7,taper_end=17,stabilization=.7)),'blue'),
    (BrushDefinition(size=12,taper_mode='percentage',taper_start=25,taper_end=40,
                     taper_parameters=('size','spacing'),post_correction=.2),'red'),
    (BrushDefinition(size=12,ribbon=True,tips=(BrushTip(shape='square',width=2,height=4),)),'blue'),
    (BrushDefinition(size=18,spray=True,particle_size=3,particle_density=3,
                     dynamics={'particle_size':BrushDynamics(random=.5)}),'red'),
    (BrushDefinition(size=12,watercolor_edge=2,watercolor_after=False),'red'),
    (BrushDefinition(size=12),'transparent'),
])
def test_cooperative_preview_is_bitwise_equal_to_public_stroke_path(brush,color):
    expected=reference_preview(brush,color)
    actual=render_brush_preview(brush,100,44,color=color)
    assert bytes(actual.constBits())==bytes(expected.constBits())


class Owner(QWidget):
    def __init__(self):
        super().__init__();self.results=[];self.visible=True
    def _preview_is_visible(self):return self.visible
    def _preview_ready(self,pixmap,error,context):self.results.append((pixmap,error,context))


@pytest.fixture
def cooperative(qapp,monkeypatch):
    queue_module.PREVIEW_CACHE.clear()
    queue=queue_module.BrushPreviewQueue()
    state={'time':0.,'steps':[],'started':[],'closed':[]}
    queue.clock=lambda:state['time']
    def slow(brush,*args):
        state['started'].append(brush.name)
        try:
            for i in range(30):
                state['time']+=.006
                state['steps'].append((brush.name,i))
                yield None
            image=QImage(args[0],args[1],QImage.Format_ARGB32);image.fill(QColor('red' if brush.name=='new' else 'blue'))
            yield image
        finally:state['closed'].append(brush.name)
    monkeypatch.setattr(queue_module,'iter_brush_preview',slow)
    yield queue,state
    queue.timer.stop()
    if queue.active:queue._close(queue.active)
    queue.pending.clear();queue.deleteLater();queue_module.PREVIEW_CACHE.clear()


def drain(queue):
    for _ in range(100):
        queue.timer.stop();queue._tick()
        if queue.active is None and not queue.pending:break
    queue.timer.stop()


def test_slow_preview_yields_to_other_qt_events_between_operations(qapp,cooperative):
    queue,state=cooperative;owner=Owner();heartbeat=[]
    queue.submit(owner,BrushDefinition(name='old'),100,44)
    QTimer.singleShot(0,lambda:heartbeat.append((len(state['steps']),len(owner.results))))
    QTest.qWait(20)
    assert heartbeat and 0<heartbeat[0][0]<30 and heartbeat[0][1]==0
    drain(queue)
    assert len(owner.results)==1 and state['closed']==['old']
    owner.deleteLater()


def test_replaced_request_is_cancelled_and_only_latest_image_arrives(cooperative):
    queue,state=cooperative;owner=Owner()
    queue.submit(owner,BrushDefinition(name='old'),100,44);queue._tick();queue.timer.stop()
    queue.submit(owner,BrushDefinition(name='new'),100,44)
    assert state['closed']==['old']
    drain(queue)
    assert len(owner.results)==1
    assert owner.results[0][0].toImage().pixelColor(0,0)==QColor('red')
    owner.deleteLater()


def test_running_preview_pauses_while_canvas_stroke_is_active(cooperative):
    queue,state=cooperative;owner=Owner();owner.canvas=SimpleNamespace(_paint_brush_stroke=None)
    queue.submit(owner,BrushDefinition(name='old'),100,44);queue._tick();queue.timer.stop()
    before=len(state['steps']);owner.canvas._paint_brush_stroke=object()
    queue._tick();queue.timer.stop()
    assert len(state['steps'])==before and not owner.results
    owner.canvas._paint_brush_stroke=None;drain(queue)
    assert len(owner.results)==1
    owner.deleteLater()


def test_only_one_iterator_retains_raster_work_and_hiding_discards_it(cooperative):
    queue,state=cooperative;a,b=Owner(),Owner()
    queue.submit(a,BrushDefinition(name='old'),100,44);queue._tick();queue.timer.stop()
    queue.submit(b,BrushDefinition(name='new'),100,44);queue._tick();queue.timer.stop()
    assert state['started']==['old']
    a.visible=False;queue._tick();queue.timer.stop()
    assert state['closed']==['old'] and state['started']==['old','new']
    drain(queue);assert not a.results and len(b.results)==1
    a.deleteLater();b.deleteLater()


def test_popup_hide_keeps_prerendering_for_the_next_open(qapp,cooperative,monkeypatch,tmp_path):
    queue,state=cooperative
    from comic_editor.ui import brush_thumbnail_store
    from comic_editor.core import settings as settings_module
    monkeypatch.setattr(brush_thumbnail_store,'preview_queue',lambda:queue)
    monkeypatch.setattr(settings_module,'settings_path',lambda:tmp_path/'settings.json')
    settings=EditorSettings(brush_presets=[BrushDefinition(name='old').to_dict()])
    combo=BrushPresetCombo(settings);combo.addItem('old',settings.brush_presets[0]['id'])
    combo.show();combo.thumbnails._prepare_next();combo.showPopup()
    queue._tick();queue.timer.stop()
    assert len(state['started'])==1
    combo.hidePopup()
    assert not state['closed'] and queue.active is not None
    drain(queue)
    assert len(state['closed'])==1 and combo.thumbnails.pixmaps
    started=list(state['started'])
    combo.showPopup();combo.hidePopup()
    assert state['started']==started
    combo.close();combo.deleteLater()


def test_edit_cancels_running_large_preview_before_debounce(qapp,cooperative,monkeypatch):
    queue,state=cooperative
    from comic_editor.ui import brush_controls
    monkeypatch.setattr(brush_controls,'preview_queue',lambda:queue)
    preview=BrushPreview();preview.show();preview.set_definition(BrushDefinition(name='old'))
    preview._request_render();queue._tick();queue.timer.stop()
    preview.set_definition(BrushDefinition(name='new'))
    assert state['closed']==['old'] and queue.active is None
    preview._request_render();drain(queue)
    assert preview.pixmap().toImage().pixelColor(0,0)==QColor('red')
    preview.close();preview.deleteLater()
