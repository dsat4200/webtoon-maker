"""Tablet terminal packets belong to their received contact, even during replay."""
from copy import copy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent

from comic_editor.core.models import ToneMask
from comic_editor.ui.canvas import ToolKind
from test_native_pointer_lifetime import hold_native_source, native_source, pointer
from test_painting_input_cursor import painting_canvas, wait_for


def target(canvas, raster, tool, mask):
    if mask:
        node = ToneMask(saved=True)
        canvas.chapter.masks[node.mask_id] = node
        canvas.set_tone_mask_mode(node.mask_id)
        raster = SimpleNamespace(object_id=node.mask_id)
    before = native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    return raster, before


def received(canvas, mask, monkeypatch):
    packets = []
    method = '_continue_mask_stroke' if mask else '_continue_stroke_impl'
    apply = getattr(canvas, method)
    def record(point, pressure):
        packets.append((point.toTuple(), pressure))
        return apply(point, pressure)
    monkeypatch.setattr(canvas, method, record)
    return packets


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('mask', [False, True])
@pytest.mark.parametrize('cold', [False, True])
def test_zero_pressure_move_preserves_last_positive_received_release_pressure(
        painting_canvas, tool, mask, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    raster, before = target(canvas, raster, tool, mask)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch) if cold else (None, None)
    if cold:
        before = QImage(str(tmp_path/'native-contact.png')).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    packets = received(canvas, mask, monkeypatch)
    send = pointer(canvas, 'pen')
    revision = canvas.command_stack.revision
    try:
        send('press', (32, 40), .2)
        if cold:
            wait_for(entered.is_set)
        send('move', (48, 40), .7, timestamp=1016)
        send('move', (72, 40), 0., timestamp=1024)
        send('release', (90, 40), timestamp=1032)
        if release is not None:
            release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        # Zero remains a received artwork sample. Only the zero-pressure lift's
        # newly delivered endpoint reuses the contact's last positive pressure.
        assert packets == [((48.,40.),.7), ((72.,40.),0.), ((90.,40.),.7)]
        image = canvas.tiles.tile(raster.object_id,(0,0))
        assert image.pixelColor(90,40) != before.pixelColor(90,40)
        assert canvas.command_stack.revision == revision+1
        canvas.command_stack.undo()
        assert canvas.tiles.tile(raster.object_id,(0,0)) == before
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('mask', [False, True])
@pytest.mark.parametrize('endpoint', [(90.,40.), (104.,80.)])
def test_older_deferred_activation_cannot_replace_newer_held_endpoint_or_pressure(
        painting_canvas, tool, mask, endpoint, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    raster, _before = target(canvas, raster, tool, mask)
    entered, release = hold_native_source(canvas,raster,tmp_path,monkeypatch)
    packets = received(canvas,mask,monkeypatch)
    send = pointer(canvas,'pen')
    deferred_name = '_mask_deferred_strokes' if mask else '_raster_deferred_strokes'
    activation_name = '_start_deferred_mask_stroke' if mask else '_start_deferred_raster_stroke'
    activate = getattr(canvas,activation_name)
    terminal_received = []
    def after_older_activation():
        activate()
        pending = getattr(canvas,deferred_name,None)
        if pending and not pending[-1]['released']:
            # Receive B's real release after replaying released contact A1.
            # A1's last endpoint is intentionally one of B's endpoint variants.
            send('move',(64,80),.9,timestamp=3024)
            send('release',endpoint,timestamp=3032)
            terminal_received.append(pending[-1]['samples'][-1])
    monkeypatch.setattr(canvas,activation_name,after_older_activation)
    revision = canvas.command_stack.revision
    try:
        send('press',(32,20),.1)
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        send('press',(32,40),.2,timestamp=2000)
        send('move',(48,40),.3,timestamp=2016)
        send('release',(90,40),timestamp=2032)
        send('press',(32,80),.7,timestamp=3000)
        send('move',(48,80),.8,timestamp=3016)
        assert len(getattr(canvas,deferred_name)) == 2
        release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None
            and not canvas._native_deferred_activations)
        assert len(terminal_received)==1
        point, pressure = terminal_received[0]
        assert point.toTuple()==endpoint and pressure==.9
        assert ((64.,80.),.9) in packets and (endpoint,.9) in packets
        assert canvas.command_stack.revision==revision+3
        assert not canvas._drawing and not canvas._pen_contact_active
    finally:
        release.set()


@pytest.mark.parametrize('tool,mask', [(ToolKind.RASTER_PENCIL,False),
    (ToolKind.RASTER_ERASER,False),(ToolKind.BRUSH,False),
    (ToolKind.RASTER_PENCIL,True),(ToolKind.RASTER_ERASER,True)])
def test_received_successor_keeps_custom_cursor_after_older_painting_finishes(
        painting_canvas, tool, mask, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    first_tool = tool if tool != ToolKind.BRUSH else ToolKind.RASTER_PENCIL
    raster, _before = target(canvas, raster, first_tool, mask)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch)
    send = pointer(canvas,'pen')
    deferred_name = ('_paint_brush_deferred' if tool == ToolKind.BRUSH else
        '_mask_deferred_strokes' if mask else '_raster_deferred_strokes')
    activation_name = '_start_deferred_mask_stroke' if mask else '_start_deferred_raster_stroke'
    activate = getattr(canvas,activation_name)
    checked, hit_queries = [], []
    def after_older_activation():
        activate()
        pending = getattr(canvas,deferred_name,None)
        if pending and not pending[-1]['released']:
            assert not canvas._drawing
            # An active modifier hit would normally select the standard move
            # cursor. The owned physical contact must resolve before any hit.
            with monkeypatch.context() as patch:
                patch.setattr(canvas, '_texture_handle_hit',
                    lambda point: (hit_queries.append(point), 'translate')[1])
                send('move',(64,80),.9,timestamp=3024)
                assert canvas.cursor().shape() == Qt.BlankCursor
                assert not hit_queries
                # A sealed journal or a changed document/store/model/tool must
                # leave ordinary hover controls usable. Restore each state
                # before the physical B release so its owner stays intact.
                contact = pending[-1]
                original_chapter, original_tiles, original_tool = canvas.chapter, canvas.tiles, canvas.tool
                sources = canvas.chapter.masks if mask else canvas.chapter.objects
                identifier = canvas.active_tone_mask_id if mask else canvas.selected_object_id
                original_source = sources[identifier]
                for retired in ('released', 'chapter', 'tiles', 'source', 'tool'):
                    try:
                        if retired == 'released':
                            contact['released'] = True
                        elif retired == 'chapter':
                            canvas.chapter = copy(original_chapter)
                        elif retired == 'tiles':
                            canvas.tiles = type(original_tiles)()
                        elif retired == 'source':
                            sources[identifier] = copy(original_source)
                        else:
                            canvas.tool = (ToolKind.RASTER_ERASER if original_tool == ToolKind.RASTER_PENCIL
                                           else ToolKind.RASTER_PENCIL)
                        hit_queries.clear()
                        canvas._update_interaction_cursor(QPointF(64,80))
                        assert canvas.cursor().shape() == Qt.SizeAllCursor
                        assert len(hit_queries) == 1
                    finally:
                        contact['released'] = False
                        canvas.chapter, canvas.tiles, canvas.tool = original_chapter, original_tiles, original_tool
                        sources[identifier] = original_source
                hit_queries.clear()
                canvas._update_interaction_cursor(QPointF(64,80))
                assert canvas.cursor().shape() == Qt.BlankCursor and not hit_queries
            checked.append(True)
            send('release',(104,80),timestamp=3032)
    monkeypatch.setattr(canvas,activation_name,after_older_activation)
    revision = canvas.command_stack.revision
    try:
        send('press',(32,20),.2)
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        send('press',(32,40),.4,timestamp=2000)
        send('release',(90,40),timestamp=2032)
        assert canvas.set_tool(tool)
        canvas.primary_color = '#ff0000ff'
        send('press',(32,80),.7,timestamp=3000)
        send('move',(48,80),.8,timestamp=3016)
        release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None
            and not canvas._native_deferred_activations)
        assert checked == [True]
        assert canvas.command_stack.revision == revision+3
        assert not canvas._drawing and not canvas._pen_contact_active
    finally:
        release.set()


@pytest.mark.parametrize('tool,mask',[(ToolKind.RASTER_PENCIL,False),(ToolKind.RASTER_ERASER,False),
    (ToolKind.BRUSH,False),(ToolKind.RASTER_PENCIL,True),(ToolKind.RASTER_ERASER,True)])
def test_mouse_hover_cannot_extend_a_released_cold_successor_journal(
        painting_canvas,tool,mask,tmp_path,monkeypatch):
    canvas,raster=painting_canvas
    raster,_before=target(canvas,raster,ToolKind.RASTER_PENCIL,mask)
    entered,release=hold_native_source(canvas,raster,tmp_path,monkeypatch)
    before=QImage(str(tmp_path/'native-contact.png')).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    send=pointer(canvas,'mouse')
    revision=canvas.command_stack.revision
    try:
        send('press',(32,20))
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        assert canvas.set_tool(tool)
        canvas.primary_color='#ff0000ff'
        send('press',(32,80),timestamp=2000)
        send('move',(48,80),timestamp=2016)
        send('release',(80,80),timestamp=2032)
        name='_paint_brush_deferred' if tool==ToolKind.BRUSH else (
            '_mask_deferred_strokes' if mask else '_raster_deferred_strokes')
        contact=getattr(canvas,name)[-1]
        journal=list(contact['samples'])
        point=QPointF(112,100)
        QCoreApplication.sendEvent(canvas,QMouseEvent(QEvent.MouseMove,point,point,
            Qt.NoButton,Qt.NoButton,Qt.NoModifier))
        assert contact['samples']==journal, 'Hover appended artwork to a received sealed contact'
        release.set()
        wait_for(lambda:canvas._native_input_predecessor() is None and not canvas._native_deferred_activations)
        assert canvas.command_stack.revision==revision+2
        assert canvas.tiles.tile(raster.object_id,(0,0)).pixelColor(112,100)==before.pixelColor(112,100)
    finally:
        release.set()


@pytest.mark.parametrize('interruption',[QEvent.WindowDeactivate,QEvent.UngrabMouse])
def test_deferred_brush_focus_loss_seals_accepted_native_samples(
        painting_canvas,interruption,tmp_path,monkeypatch):
    canvas,raster=painting_canvas
    native_source(canvas,raster,ToolKind.RASTER_PENCIL)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    entered,release=hold_native_source(canvas,raster,tmp_path,monkeypatch)
    send=pointer(canvas,'pen')
    revision=canvas.command_stack.revision
    try:
        send('press',(32,20),.2)
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        assert canvas.set_tool(ToolKind.BRUSH)
        send('press',(32,80),.7,timestamp=2000)
        send('move',(48,80),.8,timestamp=2016)
        QCoreApplication.sendEvent(canvas,QEvent(interruption))
        assert canvas._paint_brush_deferred[-1]['released']
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        release.set()
        wait_for(lambda:canvas._native_input_predecessor() is None and not canvas._native_deferred_activations)
        assert canvas.command_stack.revision==revision+2
        assert canvas._paint_brush_stroke is None
    finally:
        release.set()


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('input_kind',['mouse','pen'])
def test_native_successor_moves_belong_to_their_contact_while_older_brush_is_cold(
        painting_canvas,tool,input_kind,tmp_path,monkeypatch):
    canvas,raster=painting_canvas
    native_source(canvas,raster,ToolKind.BRUSH)
    assert canvas.set_tool(ToolKind.BRUSH)
    entered,release=hold_native_source(canvas,raster,tmp_path,monkeypatch)
    send=pointer(canvas,input_kind)
    revision=canvas.command_stack.revision
    try:
        send('press',(32,20),.2)
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        assert canvas.set_tool(tool)
        canvas.primary_color='#ff0000ff'
        send('press',(32,80),.7,timestamp=2000)
        send('move',(48,80),.8,timestamp=2016)
        send('release',(90,80),timestamp=2032)
        samples=canvas._raster_deferred_strokes[-1]['samples']
        pressure=.8 if input_kind=='pen' else 1.
        assert [(point.toTuple(),raw) for point,raw in samples]==[
            ((32.,80.),.7 if input_kind=='pen' else 1.),
            ((48.,80.),pressure),((90.,80.),pressure)]
        release.set()
        wait_for(lambda:canvas._native_input_predecessor() is None and not canvas._native_deferred_activations)
        assert canvas.command_stack.revision==revision+2
    finally:
        release.set()


@pytest.mark.parametrize('cold',[False,True])
def test_brush_zero_move_and_release_endpoint_keep_native_pressure_axes_and_time(
        painting_canvas,cold,tmp_path,monkeypatch):
    canvas,raster=painting_canvas
    before=native_source(canvas,raster,ToolKind.BRUSH)
    assert canvas.set_tool(ToolKind.BRUSH)
    entered,release=hold_native_source(canvas,raster,tmp_path,monkeypatch) if cold else (None,None)
    if cold:
        before=QImage(str(tmp_path/'native-contact.png')).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    packets=[]
    apply=canvas._apply_paint_brush_sample
    def record(sample,*,begin=False):
        packets.append(sample)
        return apply(sample,begin=begin)
    monkeypatch.setattr(canvas,'_apply_paint_brush_sample',record)
    send=pointer(canvas,'pen')
    revision=canvas.command_stack.revision
    try:
        send('press',(32,40),.2)
        if cold:
            wait_for(entered.is_set)
        send('move',(48,40),.7,timestamp=1016)
        send('move',(72,40),0.,timestamp=1024)
        send('release',(90,40),timestamp=1032)
        if release is not None:
            release.set()
        wait_for(lambda:canvas._native_input_predecessor() is None)
        assert [(sample.x,sample.y,sample.pressure) for sample in packets]==[
            (32.,40.,.2),(48.,40.,.7),(72.,40.,0.),(90.,40.,.7)]
        for sample in packets:
            assert (sample.tilt_x,sample.tilt_y,sample.rotation)==pytest.approx((12.,-7.,33.))
        assert [sample.time-packets[0].time for sample in packets]==pytest.approx([0.,.016,.024,.032])
        assert canvas.tiles.tile(raster.object_id,(0,0)).pixelColor(90,40)!=before.pixelColor(90,40)
        assert canvas.command_stack.revision==revision+1
        canvas.command_stack.undo()
        assert canvas.tiles.tile(raster.object_id,(0,0))==before
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('previous_tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('input_kind',['mouse','pen'])
def test_actual_released_brush_successor_seals_while_previous_native_source_is_cold(
        painting_canvas,previous_tool,input_kind,tmp_path,monkeypatch):
    canvas,raster=painting_canvas
    native_source(canvas,raster,previous_tool)
    assert canvas.set_tool(previous_tool)
    entered,release=hold_native_source(canvas,raster,tmp_path,monkeypatch)
    send=pointer(canvas,input_kind)
    revision=canvas.command_stack.revision
    try:
        send('press',(32,20),.2)
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        assert canvas.set_tool(ToolKind.BRUSH)
        canvas.primary_color = '#ff0000ff'
        send('press',(32,80),.7,timestamp=2000)
        send('move',(48,80),.8,timestamp=2016)
        send('release',(90,80),timestamp=2032)
        contact=canvas._paint_brush_deferred[-1]
        assert contact['released'], 'Received Brush release was owned by older Raster painting'
        point,sample=contact['samples'][-1]
        assert point.toTuple()==(90.,80.)
        assert sample.pressure==(.8 if input_kind=='pen' else 1.)
        release.set()
        wait_for(lambda:canvas._native_input_predecessor() is None and not canvas._native_deferred_activations)
        assert canvas.command_stack.revision==revision+2, [command.label for command in canvas.command_stack._undo]
        assert canvas._paint_brush_stroke is None
    finally:
        release.set()


@pytest.mark.parametrize('endpoint',[(90.,40.),(104.,80.)])
def test_older_brush_activation_preserves_newer_held_terminal_packet(
        painting_canvas,endpoint,tmp_path,monkeypatch):
    canvas,raster=painting_canvas
    native_source(canvas,raster,ToolKind.RASTER_PENCIL)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    entered,release=hold_native_source(canvas,raster,tmp_path,monkeypatch)
    send=pointer(canvas,'pen')
    activate=canvas._start_deferred_paint_brush
    received_terminal=[]
    def after_older_activation():
        activate()
        pending=canvas._paint_brush_deferred
        if pending and not pending[-1]['released']:
            send('release',endpoint,timestamp=3032)
            received_terminal.append(pending[-1]['samples'][-1])
    monkeypatch.setattr(canvas,'_start_deferred_paint_brush',after_older_activation)
    revision=canvas.command_stack.revision
    try:
        send('press',(32,20),.1)
        wait_for(entered.is_set)
        send('release',(48,20),timestamp=1016)
        assert canvas.set_tool(ToolKind.BRUSH)
        send('press',(32,40),.2,timestamp=2000)
        send('move',(48,40),.3,timestamp=2016)
        send('release',(90,40),timestamp=2032)
        send('press',(32,80),.7,timestamp=3000)
        send('move',(48,80),.8,timestamp=3016)
        before_terminal=canvas._paint_brush_deferred[-1]['samples'][-1][1]
        release.set()
        wait_for(lambda:canvas._native_input_predecessor() is None and not canvas._native_deferred_activations)
        assert len(received_terminal)==1
        point,sample=received_terminal[0]
        assert point.toTuple()==endpoint and sample.pressure==.8
        assert (sample.tilt_x,sample.tilt_y,sample.rotation)==pytest.approx((12.,-7.,33.))
        assert sample.time-before_terminal.time==pytest.approx(.016)
        assert canvas.command_stack.revision==revision+3
        assert not canvas._pen_contact_active and canvas._paint_brush_stroke is None
    finally:
        release.set()
