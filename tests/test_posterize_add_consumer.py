"""Real Add controls and detached statistics retain one guarded transaction."""
from copy import deepcopy
from threading import Event, get_ident
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QInputDialog

from comic_editor.core.commands import CallbackCommand
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, ChapterDocument, DistortModifier,
    HueSaturationLightnessModifier, PosterizeModifier, PosterizeValueModifier, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import RasterCanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls


def spin(qapp, predicate, seconds=10.):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert predicate(), 'Detached source/owner transition did not finish'


@pytest.fixture
def add_scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager',lambda *_:None)
    canvas=RasterCanvasWidget(EditorSettings(canvas_renderer='raster',snap_to_grid=False))
    chapter=ChapterDocument(width=256,height=256,document_kind='asset')
    page=chapter.add_page('Page',BoundGeometry.rectangle(0,0,256,256))
    layer=chapter.add_layer(page.layer_id,'Art',BoundGeometry.rectangle(0,0,256,256))
    obj=chapter.add_object(layer.layer_id,RasterObject(interaction_rect=(0,0,256,256)))
    tile=QImage(256,256,QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('#ff8844'))
    tiles=TileStore()
    tiles.set_tile(obj.object_id,(0,0),tile)
    twist=DistortModifier(modifier_type='distort_twirl',frame=(0,0,256,256),
        center=(128,128),radius=120,parameters={'angle':65})
    chapter.add_modifier(twist,[('object',obj.object_id)])
    chapter.add_modifier(HueSaturationLightnessModifier(hue=35),[('object',obj.object_id)])
    canvas.set_document(chapter,tiles,ImageStore())
    canvas.set_selection('object',obj.object_id)
    controls=ModifierControls(canvas)
    canvas.command_stack.push(CallbackCommand('Prior fixture command',lambda:None,lambda:None),already_done=True)
    yield canvas,controls,obj,twist
    consumers=getattr(canvas,'_scene_consumers',None)
    if consumers is not None:
        consumers.shutdown()
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
    controls.deleteLater()
    canvas.close()
    canvas.deleteLater()


def gate_statistics(monkeypatch, *, failure=False):
    from comic_editor.render import source_sampling
    original=source_sampling.posterize_statistics
    entered,release=Event(),Event()
    gui=get_ident()
    calls=[]
    def measured(snapshot,targets,before_id,value_mode=False):
        assert get_ident()!=gui, 'Statistics evaluated on the input thread'
        assert snapshot.document.pixel_contract==snapshot.chapter.pixel_contract
        calls.append((snapshot.document,targets,before_id,value_mode))
        entered.set()
        assert release.wait(10.), 'Test did not release the detached sampler'
        if failure:
            raise ValueError('intentional worker failure')
        return original(snapshot,targets,before_id,value_mode)
    monkeypatch.setattr(source_sampling,'posterize_statistics',measured)
    return entered,release,calls,original


def fresh_statistics(canvas,targets,value_mode,original):
    document=canvas._render_document_state()
    capture=canvas._scene_snapshot_compiler.capture(canvas,document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document==document
    return original(capture.result,tuple(targets),None,value_mode)


@pytest.mark.parametrize('kind',['posterize','posterize_value'])
def test_actual_count_dialog_add_yields_and_commits_one_current_palette(add_scene,qapp,monkeypatch,kind):
    canvas,controls,obj,_=add_scene
    before=deepcopy(canvas.chapter.to_dict())
    prior=tuple(canvas.command_stack._undo)
    entered,release,calls,original=gate_statistics(monkeypatch)
    expected=fresh_statistics(canvas,controls.targets(),kind=='posterize_value',original).initialize(3)
    dialogs=[]
    def accept_count():
        dialog=qapp.activeModalWidget()
        assert isinstance(dialog,QInputDialog)
        dialogs.append(True)
        dialog.setIntValue(3)
        dialog.accept()
    QTimer.singleShot(0,accept_count)
    try:
        controls.add_modifier(kind)
        spin(qapp,entered.is_set)
        assert dialogs==[True] and calls
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==prior
        assert controls._posterize_add_token is not None
        assert 'keep editing' in controls._posterize_add_status.text()
        assert controls.add_button.isEnabled()
        delivered=[]
        QTimer.singleShot(0,lambda:delivered.append(True))
        spin(qapp,lambda:bool(delivered))
        assert not release.is_set(), 'GUI delivery depended on worker completion'
        release.set()
        spin(qapp,lambda:controls._posterize_add_token is None)
        cls=PosterizeValueModifier if kind=='posterize_value' else PosterizeModifier
        added=[modifier for modifier in canvas.chapter.modifiers.values() if type(modifier) is cls]
        assert len(added)==1
        assert [(item.start,item.color) for item in added[0].ranges]==[(item.start,item.color) for item in expected]
        own=canvas.command_stack.top_undo_command
        assert own.label=='Add modifier' and len(canvas.command_stack._undo)==len(prior)+1
        assert all(a is b for a,b in zip(prior,canvas.command_stack._undo[:-1]))
        after=deepcopy(canvas.chapter.to_dict())
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==prior
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict()==after and canvas.command_stack.top_undo_command is own
    finally:
        release.set()


@pytest.mark.parametrize('change',['unsignaled-prefix','native-source','typed-prefix','history','selection','document'])
def test_pending_add_rejects_changed_input_and_never_appends_history(add_scene,qapp,monkeypatch,change):
    canvas,controls,obj,twist=add_scene
    monkeypatch.setattr(QInputDialog,'getInt',lambda *_:(3,True))
    entered,release,calls,_=gate_statistics(monkeypatch)
    try:
        controls.add_modifier('posterize')
        spin(qapp,entered.is_set)
        assert calls and controls._posterize_add_token is not None
        if change=='unsignaled-prefix':
            twist.parameters['angle']=-40
        elif change=='native-source':
            tile=QImage(256,256,QImage.Format_ARGB32_Premultiplied)
            tile.fill(QColor('blue'))
            canvas.tiles.set_tile(obj.object_id,(0,0),tile)
        elif change=='typed-prefix':
            controls.set_parameter(twist.modifier_id,'intensity',50,True)
        elif change=='history':
            canvas.command_stack.undo()
        elif change=='selection':
            canvas.set_selection('layer',obj.parent_layer_id)
        else:
            canvas.set_document(ChapterDocument.from_dict(deepcopy(canvas.chapter.to_dict())),canvas.tiles,canvas.images)
        expected=deepcopy(canvas.chapter.to_dict())
        history=tuple(canvas.command_stack._undo)
        release.set()
        spin(qapp,lambda:controls._posterize_add_token is None)
        assert 'cancelled' in controls._posterize_add_status.text()
        assert not any(isinstance(modifier,PosterizeModifier) for modifier in canvas.chapter.modifiers.values())
        assert canvas.chapter.to_dict()==expected and tuple(canvas.command_stack._undo)==history
    finally:
        release.set()


def test_second_add_has_new_ticket_after_cancelling_active_same_input(add_scene,qapp,monkeypatch):
    canvas,controls,_,_=add_scene
    counts=iter((3,4))
    monkeypatch.setattr(QInputDialog,'getInt',lambda *_:(next(counts),True))
    entered,release,calls,_=gate_statistics(monkeypatch)
    prior=tuple(canvas.command_stack._undo)
    try:
        controls.add_modifier('posterize')
        spin(qapp,entered.is_set)
        first=controls._posterize_add_token
        lane=controls._posterize_add_lane
        controls.add_modifier('posterize')
        assert controls._posterize_add_token is not first and controls._posterize_add_lane!=lane
        release.set()
        spin(qapp,lambda:controls._posterize_add_token is None)
        added=[modifier for modifier in canvas.chapter.modifiers.values() if isinstance(modifier,PosterizeModifier)]
        assert len(added)==1 and len(added[0].ranges)==4
        assert len(calls)>=2 and len(canvas.command_stack._undo)==len(prior)+1
        assert all(a is b for a,b in zip(prior,canvas.command_stack._undo[:-1]))
    finally:
        release.set()


def test_failed_detached_sampler_has_no_gui_fallback_or_partial_add(add_scene,qapp,monkeypatch):
    canvas,controls,_,_=add_scene
    monkeypatch.setattr(QInputDialog,'getInt',lambda *_:(3,True))
    before=deepcopy(canvas.chapter.to_dict())
    prior=tuple(canvas.command_stack._undo)
    entered,release,calls,_=gate_statistics(monkeypatch,failure=True)
    try:
        controls.add_modifier('posterize')
        spin(qapp,entered.is_set)
        release.set()
        spin(qapp,lambda:controls._posterize_add_token is None)
        assert calls and 'intentional worker failure' in controls._posterize_add_status.text()
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==prior
    finally:
        release.set()
