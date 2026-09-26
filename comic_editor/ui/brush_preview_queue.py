"""One cooperative raster preview at a time on the Qt event loop."""
from collections import OrderedDict
from dataclasses import fields, is_dataclass
import time
import weakref

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from comic_editor.core.brush_preview import iter_brush_preview


PREVIEW_CACHE = OrderedDict()
_QUEUE = None


def preview_key(definition, width, height, color=None, sub_color=None):
    def frozen(value, field=''):
        if field == 'png' and isinstance(value, str):
            # A process-local fingerprint avoids duplicating huge base64 strings
            # through JSON on every slider edit. Python caches a string's hash.
            return ('png', len(value), hash(value))
        if is_dataclass(value):
            return tuple((item.name,frozen(getattr(value,item.name),item.name))
                         for item in fields(value) if item.name not in {'source','warnings'})
        if isinstance(value,dict):
            return tuple((key,frozen(item,key)) for key,item in sorted(value.items()))
        if isinstance(value,(list,tuple)):
            return tuple(frozen(item) for item in value)
        return value
    return (frozen(definition),width,height,
            QColor(color).rgba() if color is not None else None,
            QColor(sub_color).rgba() if sub_color is not None else None)


def cache_preview(key, pixmap):
    PREVIEW_CACHE[key]=pixmap
    PREVIEW_CACHE.move_to_end(key)
    while len(PREVIEW_CACHE)>64:
        PREVIEW_CACHE.popitem(last=False)


def live_brush_stroke(widget):
    while widget is not None:
        canvas=getattr(widget,'canvas',None)
        if getattr(canvas,'_paint_brush_stroke',None) is not None:
            return True
        widget=widget.parentWidget()
    return False


class BrushPreviewQueue(QObject):
    """Only the active job owns a raster stroke; pending jobs own definitions."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.pending=OrderedDict()
        self.active=None
        self.timer=QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)
        self.clock=time.perf_counter
        self.max_steps=8
        self.budget_seconds=.004

    @staticmethod
    def _owner(job):
        owner=job['owner']()
        return owner if owner is not None and isValid(owner) else None

    @staticmethod
    def _close(job):
        iterator=job.get('iterator')
        if iterator is not None:
            iterator.close()
            job['iterator']=None

    def cancel(self,owner):
        self.pending.pop(id(owner),None)
        if self.active is not None and self._owner(self.active) is owner:
            self._close(self.active)
            self.active=None
        if not self.pending and self.active is None:
            self.timer.stop()

    def submit(self,owner,definition,width,height,color=None,sub_color=None,*,context=None,priority=0):
        self.cancel(owner)
        key=preview_key(definition,width,height,color,sub_color)
        if key in PREVIEW_CACHE:
            PREVIEW_CACHE.move_to_end(key)
            owner._preview_ready(PREVIEW_CACHE[key],None,context)
            return
        self.pending[id(owner)]={'owner':weakref.ref(owner),'definition':definition,
            'width':width,'height':height,'color':QColor(color) if color is not None else None,
            'sub_color':QColor(sub_color) if sub_color is not None else None,
            'key':key,'context':context,'priority':priority,'iterator':None}
        # A settings edit takes priority over a long popup thumbnail.
        if self.active is not None and priority<self.active['priority']:
            old=self.active;self._close(old);self.active=None
            owner_old=self._owner(old)
            if owner_old is not None:
                self.pending[id(owner_old)]=old
        self.timer.start(0)

    def _tick(self):
        deadline=self.clock()+self.budget_seconds
        for _ in range(self.max_steps):
            if self.active is None:
                if not self.pending:
                    return
                key=min(self.pending,key=lambda key:self.pending[key]['priority'])
                self.active=self.pending.pop(key)
            job=self.active
            owner=self._owner(job)
            if owner is None or not owner._preview_is_visible():
                self._close(job);self.active=None
                continue
            if live_brush_stroke(owner):
                self.timer.start(33)
                return
            try:
                if job['iterator'] is None:
                    job['iterator']=iter(iter_brush_preview(job['definition'],job['width'],job['height'],
                                                          job['color'],job['sub_color']))
                result=next(job['iterator'])
                if result is not None:
                    pixmap=QPixmap.fromImage(result)
                    cache_preview(job['key'],pixmap)
                    self._close(job);self.active=None
                    owner._preview_ready(pixmap,None,job['context'])
                    break
            except StopIteration:
                self._close(job);self.active=None
                owner._preview_ready(None,'Preview did not produce an image.',job['context'])
                break
            except Exception as error:
                self._close(job);self.active=None
                owner._preview_ready(None,str(error),job['context'])
                break
            if self.clock()>=deadline:
                break
        if self.active is not None or self.pending:
            self.timer.start(0)


def preview_queue():
    global _QUEUE
    if _QUEUE is None or not isValid(_QUEUE):
        _QUEUE=BrushPreviewQueue(QApplication.instance())
    return _QUEUE
