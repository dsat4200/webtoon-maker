"""Prospective ordinary MainWindow Escape routing regression; strict original model/pixels.

This extra test input preserves the six original candidate checks byte-for-byte.
It records differences before asserting them; it does not normalize model fields.
"""
from concurrent.futures import Future
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtCore import QPointF
from comic_editor.core.models import ParameterMaskBinding, ToneMask, RasterObject
from comic_editor.core.tools import ToolKind
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.main_window import MainWindow
import time


@pytest.fixture(autouse=True)
def _mask_evidence_root(tmp_path, monkeypatch):
    """Local diagnostics in ordinary pytest; the acceptance wrapper may pin a root."""
    if not os.environ.get('WEBTOON_MASK_CANCEL_EVIDENCE'):
        root = tmp_path / 'mask-escape-evidence'
        root.mkdir()
        monkeypatch.setenv('WEBTOON_MASK_CANCEL_EVIDENCE', str(root))

@pytest.fixture
def editor(qapp, tmp_path):
    repository = SeriesRepository(tmp_path / "project")
    series = repository.create("Cache test")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    chapter.height = 768
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    for point in (QPointF(90, 80), QPointF(90, 350), QPointF(90, 650)):
        tiles.paint_dab(raster.object_id, point, 32, QColor("#aa436ddd"))
    repository.save_chapter(chapter, tiles)
    windows = []
    def open_window(path=None):
        window = MainWindow()
        windows.append(window)
        window.settings.grid_overlay_visible = False
        assert window.open_series(path or repository.root)
        window.disk_cache.timer.stop()
        return window
    yield open_window(), open_window, raster.object_id
    for window in windows:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.close()
        window.deleteLater()

def wait(qapp, predicate):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.002)
    assert predicate(), "Ordinary contact/cancellation did not finish"

def begin_contact(window, qapp, kind):
    canvas = window.canvas
    canvas.settings.snap_to_grid = False
    canvas.setFixedSize(640, 480)
    canvas.scale = .4
    canvas.center_x, canvas.center_y = 540, 384
    window.show()
    if kind == "mask":
        raster = next(obj for obj in canvas.chapter.objects.values() if isinstance(obj, RasterObject))
        mask = ToneMask(saved=True, name="Status contact mask")
        canvas.chapter.masks[mask.mask_id] = mask
        raster.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
        canvas.set_tone_mask_mode(mask.mask_id)
        canvas.set_tool(ToolKind.RASTER_PENCIL)
        start, end = QPointF(90, 80), QPointF(115, 95)
    else:
        page_id = canvas.chapter.root_page_ids[0]
        canvas.set_selection("layer", page_id)
        canvas.set_tool(ToolKind.TRANSFORM)
        start, end = QPointF(540, 384), QPointF(585, 414)
    qapp.processEvents()
    window.disk_cache.timer.stop()
    before = copy.deepcopy(canvas.chapter.to_dict())
    history = tuple(canvas.command_stack._undo)
    first = canvas.document_to_widget(start).toPoint()
    last = canvas.document_to_widget(end).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, pos=first)
    QTest.mouseMove(canvas, last)
    wait(qapp, lambda: canvas._drawing if kind == "mask" else canvas._projection_has_live_preview())
    if kind == "mask":
        assert canvas._drawing and canvas._mask_tile_input is not None
    else:
        assert canvas._geometry_transform_target is not None and canvas._transform_preview_quad
        assert canvas.chapter.to_dict() == before
    return last, before, history


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stored(root):
    root=Path(root)
    return {p.relative_to(root).as_posix():sha(p) for p in sorted(root.rglob('*'))
        if p.is_file() and '__pycache__' not in p.parts and '.render-cache' not in p.parts}


def native(canvas,folder,phase):
    output={}
    for identifier,owner in tuple(canvas.tiles._tiles.items()):
        rows={}
        for key in tuple(owner):
            image=QImage(owner[key])
            raw=bytes(image.constBits())
            name=identifier+'-'+str(key[0])+'-'+str(key[1])+'.native'
            path=folder/phase/name;path.parent.mkdir(exist_ok=True)
            path.write_bytes(raw)
            rows[str(key)]=dict(key=list(key),width=image.width(),height=image.height(),
                format=image.format().value,bytes_per_line=image.bytesPerLine(),native_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),icc_sha256=hashlib.sha256(bytes(image.colorSpace().iccProfile())).hexdigest(),
                dpr=image.devicePixelRatio(),path=str(path))
        if rows:output[identifier]=rows
    return output


def exact_native(before,after):
    if before.keys()!=after.keys():return False
    metadata=('key','width','height','format','bytes_per_line','native_bytes','sha256','icc_sha256','dpr')
    for identifier,rows in before.items():
        if rows.keys()!=after[identifier].keys():return False
        for key,row in rows.items():
            other=after[identifier][key]
            if any(row[k]!=other[k] for k in metadata):return False
            if Path(row['path']).read_bytes()!=Path(other['path']).read_bytes():return False
    return True


def difference(a,b,path=''):
    if type(a) is not type(b):return [dict(path=path,before=a,after=b)]
    if isinstance(a,dict):
        result=[]
        for k in a.keys()|b.keys():
            if k not in a or k not in b:result.append(dict(path=path+'/'+str(k),before=a.get(k),after=b.get(k)))
            else:result.extend(difference(a[k],b[k],path+'/'+str(k)))
        return result
    if isinstance(a,list):
        if len(a)!=len(b):return [dict(path=path,before=a,after=b)]
        return [r for i,(x,y) in enumerate(zip(a,b)) for r in difference(x,y,path+'/'+str(i))]
    return [] if a==b else [dict(path=path,before=a,after=b)]


@pytest.mark.parametrize('completion',['ordinary','maintenance','scheduler'])
@pytest.mark.parametrize('seeded',[False,True])
def test_unchanged_R11_native_mask_Escape(editor,qapp,monkeypatch,completion,seeded):
    window,_,_=editor;canvas=window.canvas;cache=window.disk_cache
    label=completion+'-'+('seeded' if seeded else 'empty')
    folder=Path(os.environ['WEBTOON_MASK_CANCEL_EVIDENCE'])/label
    folder.mkdir(exist_ok=False)
    report=dict(completion=completion,seeded=seeded,pid=os.getpid(),status='started',scope='Isolated one-file MainWindow Escape routing; unchanged ordinary mask input and full native/model/history oracle')
    captured={};original_press=QTest.mousePress
    ended=[];original_end=canvas._end_mask_stroke_impl
    def observed_end():
        ended.append(True)
        return original_end()
    monkeypatch.setattr(canvas,'_end_mask_stroke_impl',observed_end)
    def observed_press(widget,*args,**kwargs):
        assert widget is canvas and not captured
        if seeded:
            image=QImage(canvas.tiles.tile_size,canvas.tiles.tile_size,QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor(13,71,123,37))
            canvas.tiles.set_tile(canvas.active_tone_mask_id,(0,0),image)
        captured.update(model=copy.deepcopy(canvas.chapter.to_dict()),native=native(canvas,folder,'before'),
            stored=stored(window.repository.root),mask_id=canvas.active_tone_mask_id,
            history=tuple(canvas.command_stack._undo),history_revision=canvas.command_stack.revision,
            runtime_revision=canvas._mask_runtime_revision)
        return original_press(widget,*args,**kwargs)
    monkeypatch.setattr(QTest,'mousePress',observed_press)
    try:
        last,helper_before,helper_history=begin_contact(window,qapp,'mask')
        monkeypatch.setattr(QTest,'mousePress',original_press)
        assert captured['model']==helper_before and captured['history']==helper_history
        report['before_model']=captured['model'];report['native_before']=captured['native']
        report['during_model']=copy.deepcopy(canvas.chapter.to_dict())
        report['native_during']=native(canvas,folder,'during')
        report['during_changed_native']=not exact_native(captured['native'],report['native_during'])
        report['drawing_and_owned_gate']=bool(canvas._drawing and canvas._mask_tile_input is not None)
        entries,sources=dict(cache.backing.entries),dict(cache.backing.source_digests)
        if completion=='maintenance':
            future=Future();future.set_result((entries,sources))
            cache._maintenance=future,cache._serial,'clear'
        elif completion=='scheduler':
            monkeypatch.setattr(cache.scheduler,'poll',lambda:[SimpleNamespace(
                demand=SimpleNamespace(serial=cache._serial),cache_manifest=(entries,sources),
                recorded_rows=(0,),error='',done=True,recorded=True)])
        for _ in range(4 if completion=='ordinary' else 1):cache.tick()
        report['after_live_tick_model']=copy.deepcopy(canvas.chapter.to_dict())
        QTest.keyClick(canvas,Qt.Key_Escape)
        wait(qapp,lambda:not canvas._drawing and not canvas._projection_has_live_preview())
        report['after_model']=copy.deepcopy(canvas.chapter.to_dict())
        report['native_after']=native(canvas,folder,'after')
        report['model_differences']=difference(captured['model'],report['after_model'])
        report['complete_native_before_after_equal']=exact_native(captured['native'],report['native_after'])
        report['stored_before_after_equal']=captured['stored']==stored(window.repository.root)
        report['stored_before']=captured['stored'];report['stored_after']=stored(window.repository.root)
        report['history_identity_equal']=tuple(canvas.command_stack._undo)==captured['history']
        report['history_revision_before']=captured['history_revision'];report['history_revision_after']=canvas.command_stack.revision
        report['runtime_revision_before']=captured['runtime_revision'];report['runtime_revision_after']=canvas._mask_runtime_revision
        report['gate_retired']=canvas._mask_tile_input is None
        report['native_input_error']=str(canvas._native_input_error) if canvas._native_input_error else None
        report['status']='observed'
        report['escaped_commit_calls']=len(ended)
        report['active_mask_mode_retained']=canvas.active_tone_mask_id==captured['mask_id']
        # All original cancellation semantics remain hard gates. No revision,
        # mask fields or buffers are normalized or discarded from this oracle.
        assert report['during_changed_native'] and report['drawing_and_owned_gate']
        assert report['complete_native_before_after_equal'] and report['stored_before_after_equal']
        assert report['history_identity_equal'] and report['history_revision_after']==report['history_revision_before']
        assert report['gate_retired'] and report['native_input_error'] is None
        assert report['after_model']==captured['model']
        assert report['escaped_commit_calls']==0 and report['active_mask_mode_retained']
        report['status']='passed'
    finally:
        (folder/'diagnostic.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')


@pytest.mark.parametrize('seeded',[False,True])
def test_ordinary_release_commits_one_mask_stroke_and_native_Undo_Redo(editor,qapp,monkeypatch,seeded):
    window,_,_=editor;canvas=window.canvas
    label='commit-'+('seeded' if seeded else 'empty')
    folder=Path(os.environ['WEBTOON_MASK_CANCEL_EVIDENCE'])/label;folder.mkdir(exist_ok=False)
    report=dict(status='started',pid=os.getpid(),scope='Positive ordinary release; one original mask commit and full native Undo/Redo')
    captured={};original_press=QTest.mousePress;ended=[];original_end=canvas._end_mask_stroke_impl
    def observed_end():
        ended.append(True)
        return original_end()
    monkeypatch.setattr(canvas,'_end_mask_stroke_impl',observed_end)
    def observed_press(widget,*args,**kwargs):
        assert widget is canvas and not captured
        if seeded:
            image=QImage(canvas.tiles.tile_size,canvas.tiles.tile_size,QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor(13,71,123,37));canvas.tiles.set_tile(canvas.active_tone_mask_id,(0,0),image)
        captured.update(model=copy.deepcopy(canvas.chapter.to_dict()),native=native(canvas,folder,'before'),
            history=tuple(canvas.command_stack._undo),revision=canvas.command_stack.revision,mask_id=canvas.active_tone_mask_id)
        return original_press(widget,*args,**kwargs)
    monkeypatch.setattr(QTest,'mousePress',observed_press)
    try:
        last,helper_before,helper_history=begin_contact(window,qapp,'mask')
        monkeypatch.setattr(QTest,'mousePress',original_press)
        assert helper_before==captured['model'] and helper_history==captured['history']
        QTest.mouseRelease(canvas,Qt.LeftButton,pos=last)
        wait(qapp,lambda:not canvas._drawing and canvas._mask_tile_input is None)
        committed=copy.deepcopy(canvas.chapter.to_dict());changed=native(canvas,folder,'committed')
        report.update(commit_calls=len(ended),committed_model=committed,committed_native=changed,
            model_changed=committed!=captured['model'],native_changed=not exact_native(captured['native'],changed),
            one_command=len(canvas.command_stack._undo)==len(captured['history'])+1,
            unchanged_earlier_commands=tuple(canvas.command_stack._undo[:-1])==captured['history'],
            revision_increment=canvas.command_stack.revision-captured['revision'])
        assert report['commit_calls']==1 and report['one_command'] and report['unchanged_earlier_commands']
        assert report['revision_increment']==1 and report['model_changed'] and report['native_changed']
        assert committed['masks'][0]['revision']==captured['model']['masks'][0]['revision']+1
        canvas.command_stack.undo()
        undone=copy.deepcopy(canvas.chapter.to_dict());undo_native=native(canvas,folder,'undone')
        report.update(undo_model_equal=undone==captured['model'],undo_native_equal=exact_native(captured['native'],undo_native))
        assert report['undo_model_equal'] and report['undo_native_equal']
        assert tuple(canvas.command_stack._undo)==captured['history']
        canvas.command_stack.redo()
        report.update(redo_model_equal=canvas.chapter.to_dict()==committed,redo_native_equal=exact_native(changed,native(canvas,folder,'redone')))
        assert report['redo_model_equal'] and report['redo_native_equal']
        assert len(ended)==1
        report['status']='passed'
    finally:
        (folder/'diagnostic.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
