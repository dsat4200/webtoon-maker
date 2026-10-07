"""Completed native image resources whose storage belongs to a graphics owner.

The semantic result is independent of its storage. CPU consumers explicitly
materialize it through its owner; presentation can borrow a shared texture.
Neither reference release nor object destruction calls OpenGL on the caller.
"""
from dataclasses import dataclass
import weakref
from itertools import count
from threading import Lock

from PySide6.QtCore import QSize

from comic_editor.core.pixel_contract import LEGACY_PIXELS

_VIEW_KEYS = count(1 << 48)


class _Lease:
    """Independent views share one producer pin until every view is released."""
    def __init__(self, owner, token):
        self.owner,self.token = weakref.ref(owner),token
        self.references,self.lock = 1,Lock()

    def acquire(self):
        with self.lock:
            self.references += 1

    def release(self):
        with self.lock:
            self.references -= 1
            final = self.references == 0
        owner = self.owner()
        if final and owner is not None:
            owner.release(self.token)


@dataclass(frozen=True)
class PointTableStage:
    table: object
    identity: object
    quantized_output: bool = False


@dataclass(frozen=True)
class ScalarBlurStage:
    strength: float
    algorithm: str = "normal"


@dataclass(frozen=True)
class ByteQuantizeStage:
    """The reference's truncate-to-byte boundary, followed by normalization."""


@dataclass(frozen=True)
class NativeCopyStage:
    """An integer source copy into an exact frame, with transparent padding."""
    size: tuple[int, int]
    origin: tuple[int, int]


class DeviceImage:
    """A lease for immutable, exact pixels; its texture is never caller-owned."""
    def __init__(self, owner, token, identity, width, height, texture, fence,
                 *, contract=LEGACY_PIXELS, canonical=False, _lease=None):
        self._owner = weakref.ref(owner)
        self.token, self.identity = token, identity
        self._width, self._height = int(width), int(height)
        self.texture, self.fence = int(texture), int(fence or 0)
        self.contract = contract
        self.canonical = bool(canonical)
        self.texture_width, self.texture_height = self._width,self._height
        self.source_origin = (0,0)
        self._lease = _lease or _Lease(owner,token)
        if _lease is not None:
            self._lease.acquire()
        self._cache_key = token
        self._released = False

    @property
    def owner(self):
        return self._owner()

    def width(self): return self._width
    def height(self): return self._height
    def size(self): return QSize(self._width, self._height)
    def rect(self):
        from PySide6.QtCore import QRect
        return QRect(0,0,self._width,self._height)
    def sizeInBytes(self): return self._width * self._height * 16
    def cacheKey(self): return self._cache_key
    def isNull(self):
        owner = self.owner
        return self._released or owner is None or owner.closed or not owner.available

    def materialize_pixels(self):
        owner = self.owner
        if self.isNull():
            raise RuntimeError("The graphics image owner is unavailable")
        return owner.materialize(self)

    def copy(self, rect):
        """Exact integer view; no resampling, shader execution or readback."""
        from PySide6.QtCore import QRect
        if self.isNull():
            from PySide6.QtGui import QImage
            return QImage()
        rect = QRect(rect).intersected(QRect(0,0,self._width,self._height))
        if rect.isEmpty():
            from PySide6.QtGui import QImage
            return QImage()
        value = DeviceImage(self.owner,self.token,(self.identity,'crop',tuple(rect.getRect())),
            rect.width(),rect.height(),self.texture,self.fence,contract=self.contract,
            canonical=self.canonical,_lease=self._lease)
        value._cache_key = next(_VIEW_KEYS)
        value.texture_width,value.texture_height = self.texture_width,self.texture_height
        value.source_origin = (self.source_origin[0]+rect.x(),self.source_origin[1]+rect.y())
        return value

    def materialize(self):
        from comic_editor.render.pixels import working_image
        return working_image(self.materialize_pixels(), self.contract)

    def release(self):
        if not self._released:
            self._released = True
            self._lease.release()

    def __del__(self):
        self.release()


def cpu_image(value):
    """Use only at an actual CPU image consumer, outside the GUI render path."""
    return value.materialize() if isinstance(value, DeviceImage) else value
