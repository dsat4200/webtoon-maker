"""Nested retained metadata preserves bits and separates mutable owners."""
import struct

import numpy as np
import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QColor,QImage,QTransform

from comic_editor.render.image_storage_cache import QImageStorageCache
from comic_editor.render.image_residency_pool import QImageResidencyPool
from comic_editor.render.scene import detached_value


def nest(value,depth):
    for _ in range(depth):
        value = ('native',value)
    return value


def leaf(value,depth):
    for _ in range(depth):
        value = value[1]
    return value


@pytest.mark.parametrize('depth',[1,8,40])
@pytest.mark.parametrize('segmented',[False,True])
def test_retained_key_bits_and_state_ownership(depth,segmented,qapp):
    nan = struct.unpack('d',struct.pack('Q',0x7ff8000012345678))[0]
    key = nest(('effect-tile',False,2**80,-0.,nan,b'\x00\xff'),depth)
    transform = QTransform.fromTranslate(3.,7.)
    image = QImage(4,4,QImage.Format_RGBA32FPx4_Premultiplied)
    image.fill(QColor('red'))
    array = np.array([0.,-0.,.125],dtype=np.float32)
    state = nest({'versions':[1,2],'transform':transform,'image':image,'array':array},depth)
    cache = QImageStorageCache(metadata_copy=detached_value,pool=QImageResidencyPool(segmented=segmented))
    assert cache.store('owner',image,metadata=(key,state))
    versions = leaf(state,depth)['versions']
    versions.append(3)
    transform.translate(50.,90.)
    image.fill(QColor('blue'))
    array[:] = 9.
    owned_key,owned_state = cache.metadata('owner')
    native_key = leaf(owned_key,depth)
    assert type(native_key[1]) is bool and type(native_key[2]) is int
    assert struct.pack('d',native_key[3])==struct.pack('d',-0.)
    assert struct.pack('d',native_key[4])==struct.pack('Q',0x7ff8000012345678)
    assert native_key[5]==b'\x00\xff'
    owned = leaf(owned_state,depth)
    assert owned['versions']==[1,2] and owned['transform']==QTransform.fromTranslate(3.,7.)
    assert owned['image'].pixelColor(0,0)==QColor('red')
    assert owned['array'].tobytes()==np.array([0.,-0.,.125],dtype=np.float32).tobytes()
    owned['versions'].append(99)
    assert leaf(cache.metadata('owner')[1],depth)['versions']==[1,2]


@pytest.mark.parametrize('depth',[1,8,40])
@pytest.mark.parametrize('base,value',[(int,7),(float,1.25),(str,'source'),(bytes,b'pixels')])
def test_nested_scalar_subclass_is_still_copied(depth,base,value):
    class MutableScalar(base):
        pass
    original = MutableScalar(value)
    original.state = [1,2]
    result = leaf(detached_value(nest(original,depth)),depth)
    original.state.append(3)
    assert type(result) is MutableScalar and result is not original
    assert result.state==[1,2]


@pytest.mark.parametrize('depth',[1,8,40])
@pytest.mark.parametrize('kind',['qobject','callback'])
def test_nested_editor_owner_is_rejected(depth,kind,qapp):
    owner = QObject() if kind=='qobject' else lambda: None
    with pytest.raises(TypeError,match='editor or callback'):
        detached_value(nest(owner,depth))
