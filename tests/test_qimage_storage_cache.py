"""Native QImage alias accounting and real semantic cache access checks."""
from collections import OrderedDict
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtGui import QImage

from comic_editor.render.image_storage_cache import QImageStorageCache
from comic_editor.render.scene import EvaluatedScene
from comic_editor.render.scene_kernels import SceneKernels


def image(width=4, height=4, color=0xff234567, format=QImage.Format_ARGB32_Premultiplied):
    result = QImage(width, height, format)
    result.fill(color)
    return result


def native(value):
    return value.size(), value.format(), value.bytesPerLine(), bytes(value.constBits())


def assert_owned_accounting(cache):
    handles = list(cache.values())
    unique = {int(value.cacheKey()): int(value.sizeInBytes()) for value in handles}
    assert cache.bytes == sum(unique.values())
    assert len(cache) <= cache.limit


def test_twenty_semantic_aliases_retain_one_storage_under_unchanged_budget():
    source = image()
    cache = QImageStorageCache(budget=source.sizeInBytes())
    for index in range(20):
        cache[('stage', index)] = QImage(source)
    assert len(cache) == 20 and cache.bytes == source.sizeInBytes()
    assert all(value.cacheKey() == source.cacheKey() for value in cache.values())
    assert all(native(value) == native(source) for value in cache.values())
    assert_owned_accounting(cache)


def test_equal_pixels_in_distinct_allocations_are_not_storage_aliases():
    first, second = image(), image()
    assert native(first) == native(second) and first.cacheKey() != second.cacheKey()
    cache = QImageStorageCache(budget=first.sizeInBytes() * 2)
    cache['first'], cache['second'] = first, second
    assert cache.bytes == first.sizeInBytes() + second.sizeInBytes()
    assert_owned_accounting(cache)


@pytest.mark.parametrize('edge', ['input', 'getitem', 'get', 'values', 'items'])
def test_mutating_external_handle_detaches_and_cannot_change_retained_pixels(edge):
    source = image()
    expected, original_key = native(source), source.cacheKey()
    cache = QImageStorageCache()
    cache['first'], cache['alias'] = source, QImage(source)
    borrowed = {'input': lambda: source, 'getitem': lambda: cache['first'],
        'get': lambda: cache.get('first'), 'values': lambda: next(iter(cache.values())),
        'items': lambda: next(iter(cache.items()))[1]}[edge]()
    borrowed.fill(0xffabcdef)
    assert borrowed.cacheKey() != original_key and native(borrowed) != expected
    assert native(cache['first']) == native(cache['alias']) == expected
    assert cache['first'].cacheKey() == cache['alias'].cacheKey() == original_key
    assert_owned_accounting(cache)


def test_replacement_and_last_alias_release_charge_actual_owned_storage():
    first, second = image(), image(color=0xff6789ab)
    cache = QImageStorageCache(budget=first.sizeInBytes() * 2)
    cache['first'], cache['alias'] = first, first
    cache['first'] = second
    assert cache.bytes == first.sizeInBytes() + second.sizeInBytes()
    assert native(cache['alias']) == native(first) and native(cache['first']) == native(second)
    removed = cache.pop('alias')
    assert native(removed) == native(first) and cache.bytes == second.sizeInBytes()
    cache['first'] = QImage(second)
    assert cache.bytes == second.sizeInBytes() and list(cache) == ['first']
    del cache['first']
    assert len(cache) == 0 and cache.bytes == 0
    assert native(first) == native(removed)


def test_unique_charge_prevents_alias_eviction_but_distinct_pressure_still_evicts_lru():
    first, second, third = image(), image(color=0xff56789a), image(color=0xff89abcd)
    cache = QImageStorageCache(budget=first.sizeInBytes() * 2)
    cache['stage'], cache['translation'] = first, first
    cache['other'] = second
    assert list(cache) == ['stage', 'translation', 'other']
    cache.move_to_end('stage')
    cache['new'] = third
    # Retiring only 'translation' cannot free first's storage; the ordinary
    # oldest distinct storage is evicted next, preserving first's final alias.
    assert list(cache) == ['stage', 'new']
    assert native(cache['stage']) == native(first)
    assert cache.bytes == first.sizeInBytes() + third.sizeInBytes()
    assert_owned_accounting(cache)


def test_record_limit_bounds_metadata_even_when_all_records_alias_one_image():
    source = image()
    cache = QImageStorageCache(budget=source.sizeInBytes(), limit=3)
    for index in range(100):
        cache[index] = source
    assert list(cache) == [97, 98, 99] and cache.bytes == source.sizeInBytes()
    assert_owned_accounting(cache)


def test_one_oversized_storage_admits_aliases_exclusively_and_retires_last_reference():
    oversized, small = image(), image(1, 1)
    cache = QImageStorageCache(budget=oversized.sizeInBytes() - 1)
    cache['large'], cache['alias'] = oversized, oversized
    assert list(cache) == ['large', 'alias'] and cache.bytes == oversized.sizeInBytes()
    cache['small'] = small
    assert list(cache) == ['small'] and cache.bytes == small.sizeInBytes()
    assert_owned_accounting(cache)


def test_mapping_popitem_update_clear_have_independent_owned_handles():
    first, second = image(), image(color=0xffabcdef)
    cache = QImageStorageCache()
    cache.update(OrderedDict(first=first, alias=first, second=second))
    key, removed = cache.popitem(last=False)
    assert key == 'first' and native(removed) == native(first)
    assert cache.bytes == first.sizeInBytes() + second.sizeInBytes()
    key, removed = cache.popitem()
    assert key == 'second' and native(removed) == native(second)
    assert cache.bytes == first.sizeInBytes()
    cache.clear()
    assert not cache and cache.bytes == 0 and not cache._storage
    cache['new'] = second
    assert native(cache['new']) == native(second)
    assert_owned_accounting(cache)


@pytest.mark.parametrize('format', [QImage.Format_RGB888, QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_actual_stride_format_and_precision_charge_not_width_times_assumed_rgba(format):
    source = image(3, 2, format=format)
    cache = QImageStorageCache(budget=source.sizeInBytes())
    cache['image'], cache['alias'] = source, source
    assert cache.bytes == source.sizeInBytes() == source.bytesPerLine() * source.height()
    assert native(cache['image']) == native(source)
    assert_owned_accounting(cache)


def test_null_put_preserves_previous_entry_like_existing_kernel_contract():
    source, cache = image(), QImageStorageCache()
    cache['image'] = source
    assert cache.store('image', QImage()) is False
    assert list(cache) == ['image'] and native(cache['image']) == native(source)
    assert_owned_accounting(cache)


@pytest.mark.parametrize('kind,prefix', [('effect', 'modifier_render'), ('source', 'modifier_source')])
def test_kernel_memory_and_persistent_calls_keep_original_semantic_keys_and_copy_ownership(monkeypatch, kind, prefix):
    owner = SimpleNamespace()
    cache = QImageStorageCache(budget=128)
    setattr(owner, '_' + prefix + '_cache', cache)
    setattr(owner, '_' + prefix + '_cache_bytes', 0)
    setattr(owner, '_' + prefix + '_cache_budget', 128)
    put = (SceneKernels._modifier_cache_put if kind == 'effect' else SceneKernels._modifier_source_cache_put)
    get = (SceneKernels._modifier_cache_get if kind == 'effect' else SceneKernels._modifier_source_cache_get)
    setattr(owner, '_modifier_cache_put' if kind == 'effect' else '_modifier_source_cache_put',
        MethodType(put, owner))
    puts, lookups = [], []
    restored = image(color=0xffabcdef)
    def disk_get(actual_owner, actual_kind, key):
        assert actual_owner is owner
        lookups.append((actual_kind, key))
        return restored if key == ('native-stage', 'restored') else None
    def disk_put(actual_owner, actual_kind, key, value, **kwargs):
        assert actual_owner is owner and kwargs == {}
        puts.append((actual_kind, key, native(value)))
    monkeypatch.setattr('comic_editor.ui.cache_dependencies.cache_get', disk_get)
    monkeypatch.setattr('comic_editor.ui.cache_dependencies.cache_put', disk_put)
    source = image()
    first, alias = ('native-stage', 'first'), ('translated-output', 'alias')
    put(owner, first, source)
    put(owner, alias, source)
    assert cache.bytes == source.sizeInBytes()
    assert getattr(owner, '_' + prefix + '_cache_bytes') == source.sizeInBytes()
    assert native(get(owner, first)) == native(source) and not lookups
    assert list(cache) == [alias, first]
    external = get(owner, alias)
    external.fill(0xfffedcba)
    assert native(get(owner, alias)) == native(source)
    assert native(get(owner, ('native-stage', 'restored'))) == native(restored)
    assert get(owner, ('native-stage', 'missing')) is None
    assert lookups == [(kind, ('native-stage', 'restored')), (kind, ('native-stage', 'missing'))]
    assert [item[:2] for item in puts] == [(kind, first), (kind, alias), (kind, first),
        (kind, alias), (kind, alias), (kind, ('native-stage', 'restored'))]
    assert_owned_accounting(cache)


def test_scene_adoption_rebuilds_unique_charge_and_ownership_from_legacy_snapshot():
    source = image()
    # A legacy state can overcharge aliases. Do not trust its byte summary, or
    # share an owner's mutable ledger or Python QImage wrapper across owners.
    entries = OrderedDict([(('stage', 1), source), (('translated-output', 2), QImage(source))])
    state = {'_modifier_render_cache': entries, '_modifier_render_cache_bytes': 999999,
        '_modifier_source_cache': OrderedDict([(('source', 1), source)]),
        '_modifier_source_cache_bytes': 999999}
    owner = SimpleNamespace(_modifier_render_cache_budget=64, _modifier_source_cache_budget=64)
    EvaluatedScene.adopt_cache_state(owner, state)
    assert isinstance(owner._modifier_render_cache, QImageStorageCache)
    assert owner._modifier_render_cache.bytes == owner._modifier_render_cache_bytes == 64
    assert owner._modifier_source_cache.bytes == owner._modifier_source_cache_bytes == 64
    assert list(owner._modifier_render_cache) == list(entries)
    second = SimpleNamespace(_modifier_render_cache_budget=64, _modifier_source_cache_budget=64)
    EvaluatedScene.adopt_cache_state(second, state)
    source.fill(0xffabcdef)
    assert native(owner._modifier_render_cache[('stage', 1)]) != native(source)
    assert native(second._modifier_render_cache[('stage', 1)]) != native(source)
    owner._modifier_render_cache.clear()
    assert len(second._modifier_render_cache) == 2 and second._modifier_render_cache.bytes == 64
    assert_owned_accounting(second._modifier_render_cache)


from test_detached_fit_parent_source import canvas, freeze, render, POLICIES


@pytest.mark.parametrize('contract', POLICIES, ids=['rgba8', 'float16', 'float32'])
def test_real_detached_cache_alias_transfer_and_bounded_retirement_preserve_complete_native_render(canvas, qapp, contract):
    from concurrent.futures import ThreadPoolExecutor
    import numpy as np
    from comic_editor.core.changes import ChangeSet
    from comic_editor.core.models import BoundGeometry, ImageObject, HueSaturationLightnessModifier
    from comic_editor.render.pixels import working_image
    from comic_editor.render.scene import SceneSnapshotCompiler
    chapter = canvas.chapter
    chapter.pixel_contract = contract
    parent = chapter.add_layer(chapter.root_page_ids[0], 'Native alias source',
        BoundGeometry.rectangle(8.2, 11.3, 40.1, 37.2))
    parent.fill_color, parent.border_width = None, 0
    obj = chapter.add_object(parent.layer_id, ImageObject(pixel_width=11, pixel_height=9,
        placement_mode='fit_parent', fit_mode='stretch'))
    yy, xx = np.mgrid[:9, :11]
    alpha = (.13+(xx+2*yy)%8/11).astype(np.float32)
    values = np.stack((xx/12, yy/10, ((xx+yy)%7)/8, np.ones_like(xx)), axis=-1).astype(np.float32)
    values *= alpha[..., None]
    source = working_image(values, contract)
    canvas.images.put_decoded(obj.object_id, 'native-cache-transfer', b'', source)
    original = native(canvas.images.native_image(obj.object_id))
    chapter.add_modifier(HueSaturationLightnessModifier(), [('object', obj.object_id)])
    canvas._publish_change_set(ChangeSet(conservative=True, label='Native alias fixture'))
    snapshot = freeze(canvas, SceneSnapshotCompiler(), qapp)
    with ThreadPoolExecutor(max_workers=1) as worker:
        expected, state, *_ = worker.submit(render, snapshot, obj.object_id).result(timeout=20)
        assert state['_modifier_render_cache'] and state['_modifier_source_cache']
        output = next(iter(state['_modifier_render_cache'].values()))
        aliased = dict(state)
        aliased['_modifier_render_cache'] = OrderedDict(state['_modifier_render_cache'])
        aliased['_modifier_render_cache'][('owned-alias-test', 1)] = QImage(output)
        aliased['_modifier_render_cache'][('owned-alias-test', 2)] = QImage(output)
        # Deliberately obsolete per-entry summary: adoption must rebuild it.
        aliased['_modifier_render_cache_bytes'] = 99999999
        warm, warm_state, *_ = worker.submit(render, snapshot, obj.object_id, aliased).result(timeout=20)
        assert warm == expected
        unique = {value.cacheKey(): value.sizeInBytes()
            for value in warm_state['_modifier_render_cache'].values()}
        assert warm_state['_modifier_render_cache_bytes'] == sum(unique.values())
        # Actual bounded metadata pressure retires the original semantic keys;
        # the ordinary exact renderer must recover through its usual pipeline.
        retired = dict(state)
        retired['_modifier_render_cache'] = OrderedDict(state['_modifier_render_cache'])
        original_keys = set(retired['_modifier_render_cache'])
        for index in range(540):
            retired['_modifier_render_cache'][('owned-alias-test', index)] = QImage(output)
        staged = SimpleNamespace(_modifier_render_cache_budget=67108864,
            _modifier_source_cache_budget=67108864)
        EvaluatedScene.adopt_cache_state(staged, {name: retired[name] for name in
            ('_modifier_render_cache', '_modifier_render_cache_bytes',
             '_modifier_source_cache', '_modifier_source_cache_bytes')})
        assert len(staged._modifier_render_cache) == 512
        assert original_keys.isdisjoint(staged._modifier_render_cache)
        assert staged._modifier_render_cache_bytes == output.sizeInBytes()
        retired['_modifier_render_cache'] = OrderedDict(staged._modifier_render_cache)
        rebuilt, rebuilt_state, *_ = worker.submit(render, snapshot, obj.object_id, retired).result(timeout=20)
        fresh, *_ = worker.submit(render, snapshot, obj.object_id).result(timeout=20)
    assert rebuilt == fresh == expected
    assert original == native(canvas.images.native_image(obj.object_id))
    assert len(rebuilt_state['_modifier_render_cache']) <= 512
