"""Cooperative index decoding preserves ordinary JSON and native cache results."""
import json
import sys

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRectF
from PySide6.QtGui import QColor, QColorSpace, QImage

from comic_editor.core.pixel_contract import FLOAT_PIXELS
from comic_editor.render import cache as storage


@pytest.fixture(autouse=True)
def cooperative_small_documents(monkeypatch):
    monkeypatch.setattr(storage, '_MANIFEST_COOPERATIVE_THRESHOLD', 0)


@pytest.mark.parametrize('text', [
    '{}',
    ' \t\r\n{"version":1,"entries":{},"sources":{}} \r\n',
    '{"entries":{"a":1,"b":2,"a":3},"sources":{"s":false,"s":null}}',
    '{"entries":{"discard":1},"unknown":2,"entries":[],"sources":{"old":3},'
    '"entries":{"final":4},"sources":{"new":5}}',
    '{"entries":null,"sources":[1,true,false,null],"nested":{"entries":{"x":[1,2]}}}',
    '{"entries":{"numeric":[0,-0,-0.0,1e1000,NaN,Infinity,-Infinity,3.141592653589793]},'
    '"sources":"not an object"}',
    r'{"entr\u0069es":{"\uD83D\uDE00":{"quote":"\"\\/\b\f\n\r\t","surrogate":"\uD800"}},'
    r'"sources":{"日本語":"é"},"unknown":{"arrays":[[],{},[1,{"nested":2}]]}}',
    '[]', '[{"entries":{"a":1}},NaN]', 'null', 'true', '42', '-0.0', '"a string"',
])
def test_cooperative_json_has_identical_values_types_and_key_order(text):
    expected = json.loads(text)
    actual = storage._load_cache_manifest(text)
    assert type(actual) is type(expected)
    # Canonical comparison includes dictionary order, float signed zero,
    # default NaN/Infinity handling, Unicode and unpaired surrogates.
    assert json.dumps(actual, ensure_ascii=True) == json.dumps(expected, ensure_ascii=True)


@pytest.mark.parametrize('text', [
    '', ' ', '\ufeff{}', '\u00a0{}', '{}\u00a0', '{', '{1:2}', '{,}',
    '{"entries" 1}', '{"entries":}', '{"entries":{', '{"entries":{"x":}}',
    '{"entries":{"x":1,}}', '{"entries":{"x":1,,"y":2}}', '{"entries":{},}',
    '{"entries":{"x":01}}', '{"entries":{"x":+1}}', '{"entries":{"x":NaNX}}',
    '{"entries":{"x":false true}}', '{"entries":{"x":[1,]}}',
    '{"entries":{"x":"raw\nnewline"}}', r'{"entries":{"x":"\u00xz"}}',
    '{"entries":{"x":1}} trailing', '{"entries":{}}{"sources":{}}',
    '{"entries":{}\f}', '{"sources":{"valid":{"nested":[1,2]}},"later":{invalid}',
])
def test_cooperative_json_rejects_exactly_the_same_malformed_input(text):
    with pytest.raises(json.JSONDecodeError) as ordinary:
        json.loads(text)
    with pytest.raises(json.JSONDecodeError) as cooperative:
        storage._load_cache_manifest(text)
    assert (cooperative.value.msg, cooperative.value.pos, cooperative.value.lineno,
            cooperative.value.colno) == (ordinary.value.msg, ordinary.value.pos,
                                        ordinary.value.lineno, ordinary.value.colno)


def test_small_index_keeps_the_existing_stdlib_load_path(monkeypatch):
    text = '{"entries":{},"sources":{}}'
    monkeypatch.setattr(storage, '_MANIFEST_COOPERATIVE_THRESHOLD', len(text) + 1)
    calls = []
    original = storage.json.loads
    monkeypatch.setattr(storage.json, 'loads', lambda value: calls.append(value) or original(value))
    assert storage._load_cache_manifest(text) == {'entries': {}, 'sources': {}}
    assert calls == [text]


@pytest.mark.parametrize('envelope', ['array', 'root', 'entries', 'unknown'])
@pytest.mark.parametrize('cooperative', [False, True])
@pytest.mark.parametrize('nested_type', ['array', 'object'])
def test_nested_metadata_keeps_the_stdlib_recursion_acceptance_boundary(monkeypatch, envelope, cooperative, nested_type):
    monkeypatch.setattr(storage, '_MANIFEST_COOPERATIVE_THRESHOLD', 0 if cooperative else 10 ** 9)
    def accepts(parser, depth):
        nested = ('[' * depth + '0' + ']' * depth if nested_type == 'array'
                  else '{"x":' * depth + '0' + '}' * depth)
        text = {'array': nested, 'root': '{"x":' + nested + '}',
                'entries': '{"entries":{"x":' + nested + '}}',
                'unknown': '{"unknown":{"x":' + nested + '}}'}[envelope]
        try:
            parser(text)
            return True
        except RecursionError:
            return False
    low, high = 0, sys.getrecursionlimit() * 2
    while high - low > 1:
        middle = (low + high) // 2
        if accepts(json.loads, middle):
            low = middle
        else:
            high = middle
    observed = set()
    for depth in range(low - 3, high + 4):
        expected = accepts(json.loads, depth)
        assert accepts(storage._load_cache_manifest, depth) is expected
        observed.add(expected)
    assert observed == {False, True}


@pytest.mark.parametrize('limit', [80, 160, 320])
@pytest.mark.parametrize('envelope', ['array', 'entries', 'unknown'])
@pytest.mark.parametrize('cooperative', [False, True])
@pytest.mark.parametrize('nested_type', ['array', 'object'])
def test_actual_committed_index_keeps_low_limit_recursion_behavior(tmp_path, monkeypatch, limit, envelope, cooperative, nested_type):
    parser = storage._load_cache_manifest
    monkeypatch.setattr(storage, '_MANIFEST_COOPERATIVE_THRESHOLD', 0 if cooperative else 10 ** 9)
    backing = storage.PersistentRenderCache(tmp_path)
    path = tmp_path / 'index.json'
    old_limit = sys.getrecursionlimit()
    def accepts(loader, depth):
        nested = ('[' * depth + '0' + ']' * depth if nested_type == 'array'
                  else '{"x":' * depth + '0' + '}' * depth)
        text = {'array': nested, 'entries': '{"entries":{"x":' + nested + '}}',
                'unknown': '{"unknown":{"x":' + nested + '}}'}[envelope]
        path.write_text(text)
        backing._root_state.committed = None
        monkeypatch.setattr(storage, '_load_cache_manifest', loader)
        try:
            backing._committed_index()
            return True
        except RecursionError:
            return False
    try:
        sys.setrecursionlimit(limit)
        low, high = 0, limit * 2
        while high - low > 1:
            middle = (low + high) // 2
            if accepts(json.loads, middle):
                low = middle
            else:
                high = middle
        observed = set()
        for depth in range(low - 3, high + 4):
            expected = accepts(json.loads, depth)
            assert accepts(parser, depth) is expected
            observed.add(expected)
        assert observed == {False, True}
    finally:
        sys.setrecursionlimit(old_limit)
        backing.close()


def test_one_scanner_yields_between_objects_and_returns_them_unchanged(monkeypatch):
    text = json.dumps({'entries': {str(index): {'nested': [index, {'data': 'x' * 30}]}
                                 for index in range(11)},
                       'sources': {str(index): {'stamp': [index, index + 1]} for index in range(5)},
                       'unknown': {'entries': {'opaque': [1, 2]}, 'other': [3, 4]}})
    expected = json.loads(text)
    decoded = []
    objects = []
    decoder_type = storage.json.JSONDecoder
    class ObservedDecoder(decoder_type):
        def __init__(self, **kwargs):
            hook = kwargs['object_hook']
            def observe(value):
                objects.append(value)
                assert hook(value) is value
                return value
            super().__init__(**{**kwargs, 'object_hook': observe})
        def raw_decode(self, value, idx=0):
            result, end = super().raw_decode(value, idx)
            decoded.append(value[idx:end])
            return result, end
    monkeypatch.setattr(storage.json, 'JSONDecoder', ObservedDecoder)
    monkeypatch.setattr(storage.time, 'perf_counter', lambda: 0.)
    monkeypatch.setattr(storage, '_MANIFEST_SLICE_RECORDS', 3)
    yields = []
    monkeypatch.setattr(storage.time, 'sleep', lambda seconds: yields.append((seconds, len(objects))))
    actual = storage._load_cache_manifest(text)
    assert actual == expected
    assert [count for _seconds, count in yields] == list(range(3, len(objects) + 1, 3))
    assert all(seconds == 0 for seconds, _count in yields)
    assert decoded == [text]
    assert actual is objects[-1]
    # All records share the scanner's field-name memo, as ordinary loads does.
    keys = [next(iter(record)) for record in actual['entries'].values()]
    assert all(key is keys[0] for key in keys)


@pytest.mark.parametrize('budget', ['time', 'records'])
def test_cooperation_yields_at_elapsed_or_object_budget(monkeypatch, budget):
    text = '{"entries":{"one":{"data":"' + 'x' * 100 + '"}},"sources":{}}'
    monkeypatch.setattr(storage, '_MANIFEST_SLICE_RECORDS', 1 if budget == 'records' else 10000)
    clock = iter(index * .003 if budget == 'time' else 0. for index in range(100))
    monkeypatch.setattr(storage.time, 'perf_counter', lambda: next(clock))
    yields = []
    monkeypatch.setattr(storage.time, 'sleep', lambda seconds: yields.append(seconds))
    assert storage._load_cache_manifest(text) == json.loads(text)
    assert yields and set(yields) == {0}


def _native_index(root):
    configuration = dict(contract=FLOAT_PIXELS.signature, environment=('cooperative-index', 1))
    writer = storage.PersistentRenderCache(root, **configuration)
    pixels = np.array([-.25, 1.75, np.inf, np.nan], np.float32).reshape(1, 1, 4)
    image = QImage(3, 2, QImage.Format_RGBA32FPx4_Premultiplied)
    image.fill(QColor(80, 130, 220, 110))
    image.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    image.setDevicePixelRatio(1.25)
    image.setOffset(QPoint(4, -7))
    image.bits()[4:12] = b'\x01\x02\x03\x04\x05\x06\x07\x08'
    with writer.record():
        writer.retain('effect', ('native',), pixels, state=(2, QRectF(1, 2, 3, 4)))
        writer.retain('projection', ('image',), image)
    writer.source_digests['source'] = {'stamp': [1, 2], 'pixels': ['native', [3, 4]]}
    writer.drain()
    writer.flush()
    return writer, configuration, pixels, image


def test_cooperative_pipeline_keeps_native_values_validation_and_owner_isolation(tmp_path, monkeypatch):
    writer, configuration, pixels, image = _native_index(tmp_path)
    document = json.loads((tmp_path / 'index.json').read_text())
    document['entries']['invalid'] = ['not an entry']
    storage._atomic_bytes(tmp_path / 'index.json', json.dumps(document).encode())
    validation = []
    original = storage.PersistentRenderCache._valid_entry
    monkeypatch.setattr(storage.PersistentRenderCache, '_valid_entry', staticmethod(
        lambda identity, entry: validation.append(identity) or original(identity, entry)))
    readers = []
    try:
        first = storage.PersistentRenderCache(tmp_path, **configuration)
        readers.append(first)
        second = storage.PersistentRenderCache(tmp_path, **configuration)
        readers.append(second)
        assert len(validation) == 3 and 'invalid' not in first.entries
        assert first._root_state.committed is second._root_state.committed
        assert dict(second.entries) == {key: value for key, value in document['entries'].items() if key != 'invalid'}
        first.source_digests['source']['stamp'][0] = 99
        assert second.source_digests['source'] == document['sources']['source']
        assert storage.restore_state(first.retained_state('effect', ('native',))) == (2, QRectF(1, 2, 3, 4))
        actual = second.lookup('effect', ('native',), wait=True)
        assert actual.dtype == pixels.dtype and actual.tobytes() == pixels.tobytes()
        restored = second.lookup('projection', ('image',), wait=True)
        assert (restored.format(), restored.colorSpace(), restored.devicePixelRatio(), restored.offset()) == (
            image.format(), image.colorSpace(), image.devicePixelRatio(), image.offset())
        assert bytes(restored.constBits()) == bytes(image.constBits())
        incompatible = storage.PersistentRenderCache(tmp_path, environment=('changed',))
        readers.append(incompatible)
        assert incompatible.lookup('effect', ('native',), wait=True) is None
    finally:
        for reader in readers:
            reader.close()
        writer.close()


def test_replacement_while_decoder_yields_cannot_retain_a_stale_manifest(tmp_path, monkeypatch):
    writer, configuration, _pixels, _image = _native_index(tmp_path)
    document = json.loads((tmp_path / 'index.json').read_text())
    replacement = {**document, 'entries': {}, 'sources': {'new': {'stamp': [9]}}}
    monkeypatch.setattr(storage, '_MANIFEST_SLICE_RECORDS', 1)
    replaced = []
    def replace_once(_seconds):
        if not replaced:
            replaced.append(True)
            storage._atomic_bytes(tmp_path / 'index.json', json.dumps(replacement).encode())
    monkeypatch.setattr(storage.time, 'sleep', replace_once)
    readers = []
    try:
        first = storage.PersistentRenderCache(tmp_path, **configuration)
        readers.append(first)
        assert first.entries and first._root_state.committed is None
        second = storage.PersistentRenderCache(tmp_path, **configuration)
        readers.append(second)
        assert dict(second.entries) == {}
        assert dict(second.source_digests) == replacement['sources']
    finally:
        for reader in readers:
            reader.close()
        writer.close()


def test_malformed_suffix_never_publishes_preceding_valid_native_records(tmp_path):
    writer, configuration, _pixels, _image = _native_index(tmp_path)
    text = (tmp_path / 'index.json').read_text()
    storage._atomic_bytes(tmp_path / 'index.json', (text + ' trailing').encode())
    reader = storage.PersistentRenderCache(tmp_path, **configuration)
    try:
        assert not reader.entries and not reader.source_digests
        assert reader._root_state.committed is None
    finally:
        reader.close()
        writer.close()
