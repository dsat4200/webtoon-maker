"""Shared index metadata keeps the ordinary validation and owner isolation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from threading import Event

import numpy as np
import pytest

from comic_editor.render import cache as storage
from comic_editor.core.pixel_contract import FLOAT_PIXELS, LEGACY_PIXELS
from comic_editor.ui.cache_dependencies import RenderDependencies


def seeded(root, **configuration):
    writer = storage.PersistentRenderCache(root, **configuration)
    pixels = np.array([-.25, 1.75, np.inf, np.nan], np.float32).reshape(1, 1, 4)
    with writer.record():
        writer.retain('effect', ('native',), pixels, state=[1, {'nested': [2, 3]}])
    writer.drain()
    return writer, pixels


def index_reads(monkeypatch):
    reads = []
    original = storage.json.loads
    def load(value, *args, **kwargs):
        if isinstance(value, str) and '"renderer"' in value:
            reads.append(value)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(storage.json, 'loads', load)
    return reads


def replace_index(root, document):
    storage._atomic_bytes(root / 'index.json', json.dumps(document, sort_keys=True,
        separators=(',', ':')).encode())


def test_unchanged_root_parses_and_validates_once_with_independent_native_owners(tmp_path, monkeypatch):
    writer, pixels = seeded(tmp_path)
    reads = index_reads(monkeypatch)
    validations = []
    original = storage.PersistentRenderCache._valid_entry
    def validate(identity, entry):
        validations.append(identity)
        return original(identity, entry)
    monkeypatch.setattr(storage.PersistentRenderCache, '_valid_entry', staticmethod(validate))
    readers = [storage.PersistentRenderCache(tmp_path) for _ in range(8)]
    try:
        assert len(reads) == len(validations) == 1
        for reader in readers:
            assert reader.values_lock is writer.values_lock
            assert reader.entries == writer.entries
            assert reader.entries is not writer.entries
            actual = reader.lookup('effect', ('native',), wait=True)
            assert actual.dtype == pixels.dtype and actual.tobytes() == pixels.tobytes()
        assert len(reads) == 1
    finally:
        for reader in readers:
            reader.close()
        writer.close()


@pytest.mark.parametrize('changed_dimension', ['contract', 'environment'])
def test_warmed_manifest_keeps_pixel_contract_and_environment_identity(tmp_path, monkeypatch, changed_dimension):
    configuration = dict(contract=FLOAT_PIXELS.signature, environment=('native-test', 1))
    writer, pixels = seeded(tmp_path, **configuration)
    reads = index_reads(monkeypatch)
    readers = []
    try:
        first = storage.PersistentRenderCache(tmp_path, **configuration)
        readers.append(first)
        committed = first._root_state.committed
        assert committed is not None and len(reads) == 1

        changed = dict(configuration)
        changed[changed_dimension] = (LEGACY_PIXELS.signature if changed_dimension == 'contract'
                                      else ('native-test', 2))
        incompatible = storage.PersistentRenderCache(tmp_path, **changed)
        readers.append(incompatible)
        assert incompatible._root_state.committed is committed
        assert incompatible.lookup('effect', ('native',), wait=True) is None

        matching = storage.PersistentRenderCache(tmp_path, **configuration)
        readers.append(matching)
        assert matching._root_state.committed is committed
        assert len(reads) == 1
        for reader in (first, matching):
            actual = reader.lookup('effect', ('native',), wait=True)
            assert actual.dtype == pixels.dtype and actual.tobytes() == pixels.tobytes()
    finally:
        for reader in readers:
            reader.close()
        writer.close()


def test_renderer_version_change_rejects_a_live_warmed_manifest(tmp_path, monkeypatch):
    writer, pixels = seeded(tmp_path)
    reads = index_reads(monkeypatch)
    readers = []
    try:
        first = storage.PersistentRenderCache(tmp_path)
        readers.append(first)
        state = first._root_state
        committed = state.committed
        assert committed is not None and len(reads) == 1
        generation = state.epoch, state.generation
        renderer_version = storage.RENDERER_VERSION

        with monkeypatch.context() as version_patch:
            version_patch.setattr(storage, 'RENDERER_VERSION', renderer_version + '-changed')
            incompatible = storage.PersistentRenderCache(tmp_path)
            readers.append(incompatible)
            assert incompatible.entries == {}
            assert incompatible.lookup('effect', ('native',), wait=True) is None
            assert state.committed is None and len(reads) == 2

        matching = storage.PersistentRenderCache(tmp_path)
        readers.append(matching)
        assert state.committed is not committed
        assert state.committed.versions == (storage.CACHE_VERSION, renderer_version)
        assert (state.epoch, state.generation) == generation
        assert len(reads) == 3
        for reader in (first, matching):
            actual = reader.lookup('effect', ('native',), wait=True)
            assert actual.dtype == pixels.dtype and actual.tobytes() == pixels.tobytes()
    finally:
        for reader in readers:
            reader.close()
        writer.close()


def test_owner_nested_mutations_do_not_change_a_sibling_or_committed_snapshot(tmp_path, monkeypatch):
    writer, _ = seeded(tmp_path)
    writer.source_digests['source'] = {'stamp': [1, 2], 'pixels': ['pixels', [3, 4]]}
    writer.flush()
    reads = index_reads(monkeypatch)
    first = storage.PersistentRenderCache(tmp_path)
    identity = next(iter(first.entries))
    first.entries[identity]['seal'] = 'invalid'
    first.entries[identity]['state'][1]['nested'][0] = 99
    first.source_digests['source']['stamp'][0] = 99
    first.source_digests['source']['pixels'][1][0] = 99
    second = storage.PersistentRenderCache(tmp_path)
    try:
        assert len(reads) == 1
        assert second.entries == writer.entries
        assert second.source_digests == writer.source_digests
        assert first.lookup('effect', ('native',), wait=True) is None
        assert second.lookup('effect', ('native',), wait=True) is not None
    finally:
        first.close()
        second.close()
        writer.close()


def test_concurrent_owners_share_one_parse_and_receive_independent_maps(tmp_path, monkeypatch):
    writer, _ = seeded(tmp_path)
    original = storage.json.loads
    entered, release = Event(), Event()
    reads = []
    def held(value, *args, **kwargs):
        if isinstance(value, str) and '"renderer"' in value:
            reads.append(value)
            entered.set()
            assert release.wait(5)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(storage.json, 'loads', held)
    readers = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(storage.PersistentRenderCache, tmp_path)
            assert entered.wait(2)
            second = executor.submit(storage.PersistentRenderCache, tmp_path)
            release.set()
            readers = [first.result(3), second.result(3)]
        assert len(reads) == 1
        assert readers[0].entries == readers[1].entries
        assert readers[0].entries is not readers[1].entries
        identity = next(iter(readers[0].entries))
        assert readers[0].entries[identity] is not readers[1].entries[identity]
    finally:
        release.set()
        for reader in readers:
            reader.close()
        writer.close()


@pytest.mark.parametrize('change', ['publication', 'selected-clear', 'all-clear'])
def test_root_publication_and_clear_refresh_the_manifest(tmp_path, monkeypatch, change):
    writer, pixels = seeded(tmp_path)
    reads = index_reads(monkeypatch)
    first = storage.PersistentRenderCache(tmp_path)
    try:
        assert len(reads) == 1
        if change == 'publication':
            with writer.record():
                writer.retain('effect', ('second',), pixels)
            writer.drain()
        elif change == 'selected-clear':
            with writer.record():
                writer.retain('effect', ('kept',), pixels)
            writer.drain()
            writer.clear(lambda entry: entry['key'] == ['native'])
        else:
            writer.clear()
        before = len(reads)
        second = storage.PersistentRenderCache(tmp_path)
        try:
            assert len(reads) == before + 1
            assert second.entries == writer.entries
            assert (('native',) in [tuple(value['key']) for value in second.entries.values()]) == (change == 'publication')
            if change == 'selected-clear':
                assert second.lookup('effect', ('kept',), wait=True) is not None
        finally:
            second.close()
    finally:
        first.close()
        writer.close()


def test_same_size_atomic_replacement_with_restored_mtime_refreshes_sources(tmp_path, monkeypatch):
    writer, _ = seeded(tmp_path)
    writer.source_digests['source'] = {'digest': 'a' * 64}
    writer.flush()
    reads = index_reads(monkeypatch)
    first = storage.PersistentRenderCache(tmp_path)
    try:
        path = tmp_path / 'index.json'
        before = path.stat()
        document = json.loads(path.read_text())
        document['sources']['source']['digest'] = 'b' * 64
        replace_index(tmp_path, document)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_size == before.st_size
        count = len(reads)
        second = storage.PersistentRenderCache(tmp_path)
        try:
            assert len(reads) == count + 1
            assert second.source_digests['source']['digest'] == 'b' * 64
            assert first.source_digests['source']['digest'] == 'a' * 64
        finally:
            second.close()
    finally:
        first.close()
        writer.close()


@pytest.mark.parametrize('replacement', ['corrupt', 'incompatible', 'deleted'])
def test_external_missing_or_invalid_index_does_not_reuse_old_metadata(tmp_path, replacement):
    writer, _ = seeded(tmp_path)
    first = storage.PersistentRenderCache(tmp_path)
    path = tmp_path / 'index.json'
    saved = path.read_bytes()
    if replacement == 'corrupt':
        storage._atomic_bytes(path, b'{invalid')
    elif replacement == 'incompatible':
        document = json.loads(saved)
        document['renderer'] = 'foreign-renderer'
        replace_index(tmp_path, document)
    else:
        path.unlink()
    second = storage.PersistentRenderCache(tmp_path)
    try:
        assert not second.entries
        storage._atomic_bytes(path, saved)
        third = storage.PersistentRenderCache(tmp_path)
        try:
            assert third.entries == writer.entries
            assert third.lookup('effect', ('native',), wait=True) is not None
        finally:
            third.close()
    finally:
        first.close()
        second.close()
        writer.close()


def test_replacement_during_parse_is_not_retained_for_the_next_owner(tmp_path, monkeypatch):
    writer, _ = seeded(tmp_path)
    original = storage.json.loads
    first = [True]
    def replaced(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if isinstance(value, str) and '"renderer"' in value and first[0]:
            first[0] = False
            document = dict(result, entries={})
            replace_index(tmp_path, document)
        return result
    monkeypatch.setattr(storage.json, 'loads', replaced)
    reader = storage.PersistentRenderCache(tmp_path)
    try:
        assert reader.entries
        assert reader._root_state.committed is None
        next_reader = storage.PersistentRenderCache(tmp_path)
        try:
            assert not next_reader.entries
        finally:
            next_reader.close()
    finally:
        reader.close()
        writer.close()


def test_cached_manifest_still_checks_the_blob_checksum(tmp_path, monkeypatch):
    writer, _ = seeded(tmp_path)
    reads = index_reads(monkeypatch)
    first = storage.PersistentRenderCache(tmp_path)
    entry = next(iter(first.entries.values()))
    writer._path(entry).write_bytes(b'corrupt')
    second = storage.PersistentRenderCache(tmp_path)
    try:
        assert len(reads) == 1 and second.entries
        assert second.lookup('effect', ('native',), wait=True) is None
        assert not second.entries
    finally:
        first.close()
        second.close()
        writer.close()


def test_cached_source_aliases_still_validate_changed_source_and_seal(tmp_path):
    writer, _ = seeded(tmp_path)
    source = tmp_path / 'source.bin'
    source.write_bytes(b'original')
    logical = 'tile/owner/0_0'
    dependencies = RenderDependencies(None, writer)
    original = dependencies.file_digest(logical, source)
    writer.flush()
    first = storage.PersistentRenderCache(tmp_path)
    first.source_digests[logical]['seal'] = 'corrupt'
    try:
        assert RenderDependencies(None, first).file_digest(logical, source) == original
        source.write_bytes(b'changed!')
        stamp = source.stat()
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
        second = storage.PersistentRenderCache(tmp_path)
        try:
            assert second.source_digests[logical]['digest'] == original
            assert RenderDependencies(None, second).file_digest(logical, source) == hashlib.sha256(b'changed!').hexdigest()
        finally:
            second.close()
    finally:
        first.close()
        writer.close()


def test_current_resident_aliases_merge_without_mutating_committed_sources(tmp_path):
    from comic_editor.render.resident_cache import ResidentRenderCache
    writer, _ = seeded(tmp_path)
    first = storage.PersistentRenderCache(tmp_path)
    memory = ResidentRenderCache()
    view = memory.view(str(tmp_path))
    alias = {'stamp': [1, 2, 3], 'digest': 'a' * 64}
    view.remember_source('current', alias)
    current = storage.PersistentRenderCache(tmp_path, resident=view)
    try:
        assert current.source_digests['current'] == alias
        sibling = storage.PersistentRenderCache(tmp_path)
        try:
            assert 'current' not in sibling.source_digests
        finally:
            sibling.close()
    finally:
        first.close()
        current.close()
        writer.close()


def test_last_closed_owner_releases_the_shared_manifest(tmp_path):
    writer, _ = seeded(tmp_path)
    reader = storage.PersistentRenderCache(tmp_path)
    state = reader._root_state
    assert state.committed is not None
    reader.close()
    assert state.committed is not None
    writer.close()
    assert state.committed is None and not state.owners


@pytest.mark.parametrize('route', ['getitem', 'get', 'items', 'values', 'setdefault', 'pop',
    'popitem', 'copy', 'dict', 'or', 'ror', 'update', 'ior'])
def test_all_public_value_routes_isolate_nested_private_records(route):
    base = {'first': {'nested': [1, {'value': 2}]}}
    owner = storage._OwnedManifestRecords(base)
    sibling = storage._OwnedManifestRecords(base)
    if route == 'getitem':
        value = owner['first']
    elif route == 'get':
        value = owner.get('first')
    elif route == 'items':
        value = next(iter(owner.items()))[1]
    elif route == 'values':
        value = next(iter(owner.values()))
    elif route == 'setdefault':
        value = owner.setdefault('first', {'foreign': True})
    elif route == 'pop':
        value = owner.pop('first')
    elif route == 'popitem':
        value = owner.popitem()[1]
    elif route == 'copy':
        value = owner.copy()['first']
    elif route == 'dict':
        value = dict(owner)['first']
    elif route == 'or':
        value = (owner | {'other': {}})['first']
    elif route == 'ror':
        value = ({'other': {}} | owner)['first']
    elif route == 'update':
        target = {}
        target.update(owner)
        value = target['first']
    else:
        target = storage._OwnedManifestRecords({})
        target |= owner
        value = target['first']
    value['nested'][0] = 99
    value['nested'][1]['value'] = 99
    assert base['first'] == sibling['first'] == {'nested': [1, {'value': 2}]}
    if route not in {'pop', 'popitem'}:
        assert owner['first'] is value


def test_keys_membership_and_length_remain_lazy_with_dict_order_and_assignment():
    base = {'first': [1], 'last': [2]}
    owner = storage._OwnedManifestRecords(base)
    assert list(owner) == list(owner.keys()) == ['first', 'last']
    assert list(reversed(owner)) == ['last', 'first']
    assert len(owner) == 2 and 'first' in owner and 'missing' not in owner
    assert not owner._owned
    assert owner.popitem() == ('last', [2])
    del owner['first']
    default = []
    assert owner.setdefault('first', default) is default
    owner['last'] = [3]
    assert list(owner) == ['first', 'last']
    assert owner.get('missing', default) is default
    assert owner.pop('missing', default) is default
    assert base == {'first': [1], 'last': [2]}
    owner.clear()
    assert owner == {} and owner._base == {}
    with pytest.raises(KeyError):
        owner.popitem()
    with pytest.raises(TypeError):
        owner | []


def test_record_manifest_returned_maps_survive_closed_root_and_keep_isolation(tmp_path):
    from types import SimpleNamespace
    from comic_editor.render.scene import DetachedSceneBackend
    writer, pixels = seeded(tmp_path)
    manifest = DetachedSceneBackend.record_manifest(SimpleNamespace(
        scene=SimpleNamespace(_persistent_render_cache=writer)))
    state = writer._root_state
    writer.close()
    assert state.committed is None and not state.owners
    entries, sources = manifest
    identity = next(iter(entries))
    assert entries[identity]['state'] == [1, {'nested': [2, 3]}]
    entries[identity]['state'][1]['nested'][0] = 99
    sources['new'] = {'nested': [99]}
    reader = storage.PersistentRenderCache(tmp_path)
    try:
        assert reader.entries[identity]['state'] == [1, {'nested': [2, 3]}]
        assert 'new' not in reader.source_digests
        assert reader.lookup('effect', ('native',), wait=True).tobytes() == pixels.tobytes()
    finally:
        reader.close()


def test_explicit_json_publication_materializes_lazy_records_without_private_aliases(tmp_path):
    writer, pixels = seeded(tmp_path)
    reader = storage.PersistentRenderCache(tmp_path)
    try:
        assert not reader.entries._owned
        encoded = reader._index_bytes(reader.entries)
        document = json.loads(encoded)
        identity = next(iter(document['entries']))
        document['entries'][identity]['state'][1]['nested'][0] = 99
        assert reader.entries[identity]['state'][1]['nested'][0] == 2
        with reader.record():
            reader.retain('effect', ('second',), pixels)
        reader.drain()
        reopened = storage.PersistentRenderCache(tmp_path)
        try:
            assert len(reopened.entries) == 2
            assert reopened.lookup('effect', ('native',), wait=True).tobytes() == pixels.tobytes()
            assert reopened.lookup('effect', ('second',), wait=True).tobytes() == pixels.tobytes()
        finally:
            reopened.close()
    finally:
        reader.close()
        writer.close()


def test_deferred_gui_binding_does_not_wait_for_a_siblings_index_parser(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    writer, _ = seeded(tmp_path)
    original = storage.json.loads
    entered, release = Event(), Event()
    first = [True]
    def held(value, *args, **kwargs):
        if isinstance(value, str) and '"renderer"' in value and first[0]:
            first[0] = False
            entered.set()
            assert release.wait(5)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(storage.json, 'loads', held)
    owner = deferred = None
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(storage.PersistentRenderCache, tmp_path)
            assert entered.wait(2)
            # The parser owns values_lock. A GUI bind only registers its owner
            # and schedules IO, so this call must finish while it stays held.
            deferred = storage.PersistentRenderCache(tmp_path, defer_index=True)
            assert deferred.index_loading and not release.is_set()
            callbacks = []
            QTimer.singleShot(0, lambda: callbacks.append(True))
            qapp.processEvents()
            assert callbacks == [True]
            release.set()
            owner = pending.result(3)
        deferred._reload.result(3)
        deferred.poll()
        assert deferred.entries == owner.entries
    finally:
        release.set()
        if owner is not None:
            owner.close()
        if deferred is not None:
            deferred.close()
        writer.close()
