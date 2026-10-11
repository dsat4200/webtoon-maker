"""Immutable key leaves can be reused without sharing mutable scene state."""
from dataclasses import dataclass
import math
import struct

import pytest
from PySide6.QtCore import QObject, QPointF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.render.scene import detached_value


@pytest.mark.parametrize('value', [None, False, True, 0, -1, 2**80, 0., -0.,
    float('inf'), float('-inf'), float('nan'), 'native-key', b'\x00\xff'])
def test_scalar_types_and_float_bits_are_preserved(value):
    result = detached_value(value)
    assert type(result) is type(value)
    if type(value) is float:
        assert struct.pack('d', result) == struct.pack('d', value)
    else:
        assert result == value


def test_nested_immutable_key_keeps_type_distinctions_and_signed_zero():
    key = ('tile-output', ('native', False, 0, 0., -0., b'pixel-contract'),
        (('source', 2**80), ('frame', 1., 2., 3., 4.)))
    result = detached_value(key)
    assert result == key
    assert [type(item) for item in result[1][1:5]] == [bool, int, float, float]
    assert math.copysign(1., result[1][4]) == -1.


@pytest.mark.parametrize('base,value', [(int, 7), (float, 1.25), (str, 'source'), (bytes, b'source')])
def test_scalar_subclasses_keep_their_independent_mutable_attributes(base, value):
    class OwnedScalar(base):
        pass
    source = OwnedScalar(value)
    source.state = {'versions': [1, 2]}
    result = detached_value((source,))[0]
    assert type(result) is OwnedScalar and result == source
    source.state['versions'].append(3)
    assert result.state == {'versions': [1, 2]}
    assert result is not source


def test_nested_mutable_containers_and_qt_geometry_remain_detached():
    transform = QTransform.fromTranslate(3., 5.)
    point = QPointF(7., 11.)
    source = ({'items': [transform, {'point': point}], 'members': {1, 2}},)
    result = detached_value(source)
    transform.translate(10., 20.)
    point.setX(99.)
    source[0]['members'].add(3)
    source[0]['items'].append('new')
    assert len(result[0]['items']) == 2
    assert result[0]['items'][0] == QTransform.fromTranslate(3., 5.)
    assert result[0]['items'][1]['point'] == QPointF(7., 11.)
    assert result[0]['members'] == {1, 2}


def test_dataclass_and_qimage_keep_copy_on_write_ownership(qapp):
    @dataclass
    class State:
        values: dict
        image: QImage
    image = QImage(4, 4, QImage.Format_RGBA32FPx4_Premultiplied)
    image.fill(QColor('red'))
    source = State({'versions': [1]}, image)
    result = detached_value((source,))[0]
    expected = bytes(result.image.constBits())
    source.values['versions'].append(2)
    image.fill(QColor('blue'))
    assert result.values == {'versions': [1]}
    assert bytes(result.image.constBits()) == expected
    assert result.image.format() == QImage.Format_RGBA32FPx4_Premultiplied
    assert bytes(source.image.constBits()) != expected


@pytest.mark.parametrize('container', [lambda value: value, lambda value: (value,),
    lambda value: {'nested': [value]}])
@pytest.mark.parametrize('kind', ['qobject', 'callback'])
def test_editor_owners_and_callbacks_are_rejected_even_when_nested(container, kind, qapp):
    value = QObject() if kind == 'qobject' else lambda: None
    with pytest.raises(TypeError, match='editor or callback'):
        detached_value(container(value))
