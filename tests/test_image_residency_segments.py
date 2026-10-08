"""Native controls for qualified reuse and bounded probation/protection.

Prepared only. Root must run these with the frozen native actor before apply.
No timing, full working-set fit or process-memory claim follows from them.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import gc
from types import SimpleNamespace
import weakref

import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.render.image_residency_pool import QImageResidencyPool
from comic_editor.render.image_storage_cache import QImageStorageCache
from comic_editor.render.scene import DetachedSceneBackend, InlineResults
from test_inline_result_storage import FORMATS, image, native, scene
from test_qimage_residency_pool import accounting, exact_render
from test_detached_fit_parent_source import canvas, freeze, POLICIES


def roles(budget, limit=512):
    pool = QImageResidencyPool(segmented=True)
    source = QImageStorageCache(budget, limit, pool=pool)
    effect = QImageStorageCache(budget, limit, pool=pool)
    jobs = InlineResults(SimpleNamespace(_persistent_render_cache=None), budget, limit, pool=pool)
    return pool, source, effect, jobs


def segments(pool, *maps):
    assert pool.protected_bytes == sum(pool._nodes[key][1] for key in pool._protected)
    assert pool.protected_bytes <= pool.protected_budget
    assert set(pool._protected) <= set(pool._nodes)
    for mapping in maps:
        assert len(mapping._protected_aliases) <= mapping.protected_limit
        assert set(mapping._protected_aliases) <= set(mapping)
        assert all(mapping._entries[key][0] in pool._protected for key in mapping._protected_aliases)


@pytest.mark.parametrize('format', FORMATS)
def test_qualified_hit_preserves_native_node_and_all_selected_role_aliases_under_scan(format):
    warm = image(format)
    expected = native(warm)
    pool, source, effect, jobs = roles(warm.sizeInBytes(), limit=6)
    source['source'], effect['stack'], effect['output'] = warm, warm, warm
    jobs.retained_put('pipeline', 'semantic', warm, {'position': [1.25, -2.5]})
    assert source.reuse('source') and effect.reuse('output')
    assert jobs.retained_get('pipeline', 'semantic')[1]['position'] == [1.25, -2.5]
    # Cold allocations exceed the complete three-role allowance repeatedly.
    # Cheap aliases then exceed both role metadata allowances.
    for index in range(12):
        cold = image(format, QColor(index + 3, 37, 71, 201))
        source[index], effect[index] = cold, cold
        jobs.retained_put(index, ('scan', index), cold)
    # The distinct-allocation phase primarily applies byte pressure. Now
    # exceed six metadata records using only aliases of already owned storage.
    # This phase admits no new native allocation and no additional pixel bytes.
    assert 'stack' in effect and 'stack' not in effect._protected_aliases
    metadata_storage = int(cold.cacheKey())
    assert metadata_storage in pool._nodes
    metadata_nodes, metadata_bytes = pool.node_count, pool.bytes
    for index in range(8):
        alias = QImage(cold)
        assert int(alias.cacheKey()) == metadata_storage
        source[('alias', index)], effect[('alias', index)] = alias, alias
        jobs.retained_put(('alias', index), ('scan-alias', index), alias)
        assert int(source.peek(('alias', index)).cacheKey()) == metadata_storage
        assert int(effect.peek(('alias', index)).cacheKey()) == metadata_storage
        assert int(jobs.retained[('alias', index)][1].cacheKey()) == metadata_storage
        assert pool.node_count <= metadata_nodes and pool.bytes <= metadata_bytes
        assert len(source) <= source.limit and len(effect) <= effect.limit and len(jobs.retained) <= jobs.limit
    assert native(source.peek('source')) == native(effect.peek('output')) == expected
    assert native(jobs.retained['pipeline'][1]) == expected
    assert 'stack' not in effect  # Unread alias has ordinary metadata priority.
    accounting(pool, source, effect, jobs)
    segments(pool, source, effect, jobs._pooled_images)
    assert native(warm) == expected


def test_actual_192mib_512_records_admit_scan_without_retiring_reused_pixels():
    owner = scene()
    pool = owner._image_residency_pool
    source, effect, jobs = owner._modifier_source_cache, owner._modifier_render_cache, owner._effect_jobs
    assert pool.budget == 192 * 1024 * 1024 and source.limit == effect.limit == jobs.limit == 512
    warm = image(FORMATS[0], width=2048, height=2048)
    original = native(warm)
    owner._modifier_source_cache_put('warm-source', warm)
    owner._modifier_cache_put('warm-output', warm)
    jobs.retained_put('warm-scope', 'warm-key', warm)
    assert owner._modifier_source_cache_get('warm-source') is not None
    assert owner._modifier_cache_get('warm-output') is not None
    assert jobs.retained_get('warm-scope', 'warm-key') is not None
    for index in range(60):
        cold = image(FORMATS[0], QColor(index + 1, 19, 83), width=1024, height=1024)
        source[('node', index)], effect[('node', index)] = cold, cold
    # Only metadata pressure here: all scan aliases share one owned allocation.
    for index in range(600):
        source[('alias', index)], effect[('alias', index)] = cold, cold
        jobs.retained_put(('alias', index), ('key', index), cold)
    assert native(source.peek('warm-source')) == native(effect.peek('warm-output')) == original
    assert native(jobs.retained['warm-scope'][1]) == original
    assert pool.protected_budget == 128 * 1024 * 1024
    assert source.protected_limit == effect.protected_limit == jobs._pooled_images.protected_limit == 341
    accounting(pool, source, effect, jobs)
    segments(pool, source, effect, jobs._pooled_images)
    pool.clear()
    assert pool.bytes == pool.protected_bytes == pool.alias_count == 0


@pytest.mark.parametrize('operation', ['get', 'getitem', 'move', 'peek', 'items', 'values',
    'export', 'metadata', 'reorder', 'refresh', 'new-alias', 'state', 'adopt'])
def test_bookkeeping_and_refresh_never_create_protection(operation):
    owner = scene()
    mapping = owner._modifier_render_cache
    original = image(FORMATS[0])
    mapping['key'] = original
    operations = {
        'get': lambda: mapping.get('key'), 'getitem': lambda: mapping['key'],
        'move': lambda: mapping.move_to_end('key'), 'peek': lambda: mapping.peek('key'),
        'items': lambda: tuple(mapping.items()), 'values': lambda: tuple(mapping.values()),
        'export': mapping.export, 'metadata': lambda: mapping.metadata('key'),
        'reorder': lambda: mapping.reorder(('key',)), 'refresh': lambda: mapping.store('key', original),
        'new-alias': lambda: mapping.store('alias', original), 'state': owner.cache_state,
        'adopt': lambda: owner.adopt_cache_state(owner.cache_state()),
    }
    operations[operation]()
    assert owner._image_residency_pool.protected_node_count == 0 and not mapping._protected_aliases


@pytest.mark.parametrize('format', FORMATS)
def test_refresh_preserves_qualified_history_but_new_allocation_starts_probation(format):
    original = image(format)
    pool, source, effect, jobs = roles(original.sizeInBytes())
    source['key'] = original
    source.reuse('key')
    before = (tuple(pool._protected), tuple(source._protected_aliases), pool.protected_bytes)
    source.store('key', QImage(original))
    assert (tuple(pool._protected), tuple(source._protected_aliases), pool.protected_bytes) == before
    independent = original.copy()
    assert native(independent) == native(original) and independent.cacheKey() != original.cacheKey()
    source.store('key', independent)
    assert not pool._protected and not source._protected_aliases
    source.store('alias', independent)
    assert not source._protected_aliases
    accounting(pool, source, effect, jobs)


def test_scope_mismatch_is_not_a_hit_and_null_semantic_replacement_has_no_stale_state(monkeypatch):
    owner = scene()
    jobs = owner._effect_jobs
    pool = owner._image_residency_pool
    monkeypatch.setattr('comic_editor.ui.cache_dependencies.cache_get', lambda *args: None)
    original = image(FORMATS[0])
    jobs.retained_put('scope', 'old', original, {'placement': [1, 2]})
    assert jobs.retained_get('scope', 'other') is None and not pool._protected
    assert jobs.retained_get('scope', 'old') is not None
    assert 'scope' in jobs._pooled_images._protected_aliases
    jobs.retained_put('scope', 'old', QImage(), {'placement': [3, 4]})
    value, state = jobs.retained_get('scope', 'old')
    assert value.isNull() and state == {'placement': [3, 4]} and pool.bytes == 0
    jobs.retained_put('scope', 'new', QImage(), {'placement': [5, 6]})
    assert 'scope' not in jobs._pooled_images._protected_aliases
    assert jobs.retained_get('scope', 'old') is None
    value, state = jobs.retained_get('scope', 'new')
    state['placement'][0] = 99
    assert value.isNull() and jobs.retained['scope'][2] == {'placement': [5, 6]}
    segments(pool, jobs._pooled_images)


@pytest.mark.parametrize('role', ['source', 'effect'])
def test_disk_restore_is_first_admission_then_resident_hit_promotes(monkeypatch, role):
    owner = scene()
    original = image(FORMATS[0])
    calls = []
    monkeypatch.setattr('comic_editor.ui.cache_dependencies.cache_get',
        lambda *args: calls.append(args[1:]) or QImage(original))
    monkeypatch.setattr('comic_editor.ui.cache_dependencies.cache_put', lambda *args: None)
    getter = owner._modifier_source_cache_get if role == 'source' else owner._modifier_cache_get
    mapping = owner._modifier_source_cache if role == 'source' else owner._modifier_render_cache
    assert native(getter('semantic')) == native(original)
    assert not mapping._protected_aliases and not owner._image_residency_pool._protected
    assert native(getter('semantic')) == native(original)
    assert tuple(mapping._protected_aliases) == ('semantic',) and len(calls) == 1


@pytest.mark.parametrize('format', FORMATS)
def test_protected_byte_spill_demotes_all_aliases_and_retirement_remains_atomic(format):
    first, second, third, fourth = (image(format, QColor(color)) for color in ('red', 'green', 'blue', 'yellow'))
    pool, source, effect, jobs = roles(first.sizeInBytes(), limit=6)
    source['first'], effect['first'] = first, first
    jobs.retained_put('first', 'key', first)
    source.reuse('first'); effect.reuse('first'); jobs.retained_get('first', 'key')
    source['second'], effect['third'] = second, third
    source.reuse('second'); effect.reuse('third')
    assert first.cacheKey() not in pool._protected
    assert 'first' not in source._protected_aliases and 'first' not in effect._protected_aliases
    assert 'first' not in jobs._pooled_images._protected_aliases
    effect['fourth'] = fourth
    assert 'first' not in source and 'first' not in effect and not jobs.retained
    accounting(pool, source, effect, jobs)
    segments(pool, source, effect, jobs._pooled_images)


def test_metadata_protection_spills_at_two_thirds_without_byte_demotion():
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes(), limit=3)
    for key in ('first', 'second', 'third'):
        source[key] = original
        source.reuse(key)
    assert tuple(source._protected_aliases) == ('second', 'third')
    source['fourth'] = original
    assert 'first' not in source and set(source) == {'second', 'third', 'fourth'}
    source.limit = 1
    assert not source._protected_aliases and len(source) == 1
    assert pool.protected_node_count == 1
    pool.clear()
    assert not pool._protected and not source._protected_aliases


@pytest.mark.parametrize('format', FORMATS)
def test_export_transfer_preserves_segment_orders_and_owned_pixels_then_old_snapshot_is_probation(format):
    owner = scene()
    source, effect, jobs = owner._modifier_source_cache, owner._modifier_render_cache, owner._effect_jobs
    originals = [image(format, QColor(color)) for color in ('red', 'green', 'blue')]
    expected = [native(value) for value in originals]
    for index, value in enumerate(originals):
        source[index], effect[index] = value, value
        jobs.retained_put(index, ('key', index), value, {'placement': [index, 1.25]})
    for index in (2, 0, 1):
        owner._modifier_source_cache_get(index)
        owner._modifier_cache_get(index)
        jobs.retained_get(index, ('key', index))
    state = owner.cache_state()
    restored = scene()
    restored.adopt_cache_state(state)
    assert tuple(restored._image_residency_pool._protected) == tuple(owner._image_residency_pool._protected)
    for mapping, adopted in zip((source, effect, jobs._pooled_images),
        (restored._modifier_source_cache, restored._modifier_render_cache, restored._effect_jobs._pooled_images)):
        assert tuple(adopted._protected_aliases) == tuple(mapping._protected_aliases)
        assert tuple(adopted) == tuple(mapping)
    state['image_reuse_state']['nodes'][0].fill(QColor('yellow'))
    state['_modifier_source_cache'][0].fill(QColor('yellow'))
    state['retained'][0][2]['placement'][0] = 99
    borrowed = restored._modifier_cache_get(0)
    borrowed.fill(QColor('yellow'))
    assert native(restored._modifier_render_cache.peek(0)) == expected[0]
    assert restored._effect_jobs.retained[0][2]['placement'][0] == 0
    assert [native(value) for value in originals] == expected
    old = owner.cache_state()
    old.pop('image_reuse_policy'); old.pop('image_reuse_state')
    restored.adopt_cache_state(old)
    assert not restored._image_residency_pool._protected
    assert all(not mapping._protected_aliases for mapping in (restored._modifier_source_cache,
        restored._modifier_render_cache, restored._effect_jobs._pooled_images))


def test_transfer_recomputes_charges_filters_changed_handle_and_spills_smaller_target():
    owner = scene()
    original = image(FORMATS[0])
    for index in range(3):
        owner._modifier_cache_put(index, original.copy())
        owner._modifier_cache_get(index)
    state = owner.cache_state()
    state['_modifier_render_cache_bytes'] = state['retained_bytes'] = 10 ** 12
    # A detached history handle is modified, leaving the cache image unchanged.
    state['image_reuse_state']['nodes'][0].fill(QColor('red'))
    receiver = scene()
    size = original.sizeInBytes()
    receiver._modifier_source_cache.budget = receiver._modifier_render_cache.budget = size
    receiver._effect_jobs.budget = size
    receiver.adopt_cache_state(state)
    assert receiver._image_residency_pool.bytes == 3 * size
    assert receiver._image_residency_pool.protected_bytes == 2 * size
    segments(receiver._image_residency_pool, receiver._modifier_render_cache)
    receiver._modifier_source_cache.budget = receiver._modifier_render_cache.budget = 0
    receiver._effect_jobs.budget = 0
    assert not receiver._image_residency_pool._protected
    assert receiver._image_residency_pool.pixel_node_count == 1
    receiver._image_residency_pool.clear()


@pytest.mark.parametrize('format', FORMATS)
def test_transfer_changed_retained_identity_is_probation_and_history_survives_owner_close(format):
    owner = scene()
    original = image(format)
    owner._effect_jobs.retained_put('scope', ('old', 1), original, {'placement': [1, 2]})
    owner._effect_jobs.retained_get('scope', ('old', 1))
    exported = owner.cache_state()
    scope_key, value, _state, size = exported['retained']['scope']
    assert scope_key == ('old', 1)
    exported['retained']['scope'] = ('new', 2), value, {'placement': [3, 4]}, size
    owner._image_residency_pool.clear()
    assert owner._image_residency_pool.protected_bytes == 0
    receiver = scene()
    receiver.adopt_cache_state(exported)
    assert 'scope' not in receiver._effect_jobs._pooled_images._protected_aliases
    assert receiver._effect_jobs.retained['scope'][0] == ('new', 2)
    assert native(receiver._effect_jobs.retained['scope'][1]) == native(original)
    assert receiver._effect_jobs.retained_get('scope', ('new', 2))[1] == {'placement': [3, 4]}
    DetachedSceneBackend.close(SimpleNamespace(scene=receiver))
    assert receiver._image_residency_pool.bytes == receiver._image_residency_pool.protected_bytes == 0
    assert native(exported['retained']['scope'][1]) == native(original)


def test_equal_distinct_key_refresh_preserves_resident_descriptor_and_original_history():
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes())
    canonical = tuple(['semantic', 1, 2])
    incoming = tuple(['semantic', 1, 2])
    assert canonical == incoming and canonical is not incoming
    source[canonical] = original
    source.reuse(canonical)
    source.store(incoming, QImage(original))
    assert next(iter(source._entries)) is canonical
    assert tuple(source._protected_aliases) == (canonical,)
    assert pool.bytes == pool.protected_bytes == original.sizeInBytes()
    del source[incoming]
    assert not source and not pool._nodes and not source._protected_aliases


@pytest.mark.parametrize('fault', ['policy', 'missing', 'duplicate-node', 'duplicate-alias', 'alias-shape', 'callback', 'unhashable'])
def test_malformed_transfer_rejects_before_releasing_existing_owner(fault):
    owner = scene()
    original = image(FORMATS[0])
    owner._modifier_cache_put('keep', original)
    owner._modifier_cache_get('keep')
    state = owner.cache_state()
    reuse = state['image_reuse_state']
    if fault == 'policy': state['image_reuse_policy'] = ('unknown', 7)
    elif fault == 'missing': state.pop('image_reuse_state')
    elif fault == 'duplicate-node': reuse['nodes'] *= 2
    elif fault == 'duplicate-alias': reuse['aliases']['effect'] *= 2
    elif fault == 'alias-shape': reuse['aliases']['effect'] = ((1, original),)
    elif fault == 'callback': reuse['aliases']['effect'] = (('keep', original, lambda: None),)
    elif fault == 'unhashable': reuse['aliases']['effect'] = (([], original, None),)
    before = (owner._image_residency_pool.bytes, tuple(owner._image_residency_pool._protected),
        tuple(owner._modifier_render_cache._protected_aliases), native(owner._modifier_render_cache.peek('keep')))
    with pytest.raises(ValueError): owner.adopt_cache_state(state)
    assert (owner._image_residency_pool.bytes, tuple(owner._image_residency_pool._protected),
        tuple(owner._modifier_render_cache._protected_aliases), native(owner._modifier_render_cache.peek('keep'))) == before


def test_oversized_and_null_are_bounded_progress_exceptions_without_hard_pins():
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes())
    source['first'] = original
    source.reuse('first')
    oversized = image(FORMATS[0], width=68, height=44)
    effect['exclusive'] = oversized
    assert 'first' not in source and pool.bytes > pool.budget and pool.pixel_node_count == 1
    assert not effect.reuse('exclusive') and not pool._protected
    jobs.retained_put('null', 'null-key', QImage(), {'state': 1})
    assert jobs.retained_get('null', 'null-key')[0].isNull()
    assert native(effect.peek('exclusive')) == native(oversized)
    assert not jobs.retained_put('reject', 'too-large', oversized)
    source['next'] = original
    assert 'exclusive' not in effect
    accounting(pool, source, effect, jobs)


def test_mapping_retirement_releases_contribution_and_protection_without_owner_cycle():
    pool = QImageResidencyPool(segmented=True)
    original = image(FORMATS[0])
    source = QImageStorageCache(original.sizeInBytes(), pool=pool)
    effect = QImageStorageCache(original.sizeInBytes(), pool=pool)
    source['source'] = original
    source.reuse('source')
    reference = weakref.ref(source)
    del source
    gc.collect()
    assert reference() is None and pool.bytes == pool.protected_bytes == pool.alias_count == 0
    assert pool.budget == original.sizeInBytes() and not pool._protected
    effect.clear()


def test_standalone_and_default_pool_keep_existing_lru_contract():
    original = image(FORMATS[0])
    standalone = QImageStorageCache(original.sizeInBytes(), limit=2)
    standalone['first'], standalone['second'] = original, original
    assert not standalone.reuse('first')
    standalone['third'] = original
    assert tuple(standalone) == ('second', 'third')
    legacy = QImageResidencyPool()
    mapping = QImageStorageCache(original.sizeInBytes(), limit=2, pool=legacy)
    mapping['first'], mapping['second'] = original, original
    assert not mapping.reuse('first') and not legacy.segmented
    mapping['third'] = original
    assert tuple(mapping) == ('second', 'third') and legacy.protected_bytes == 0


@pytest.mark.parametrize('contract', POLICIES, ids=['rgba8', 'float16', 'float32'])
@pytest.mark.parametrize('mapping', ['ordinary', 'projective'])
def test_complete_native_cold_warm_segment_transfer_eviction_and_changed_key_keep_source_bits(canvas, qapp, contract, mapping):
    import numpy as np
    from comic_editor.core.changes import ChangeSet
    from comic_editor.core.models import BoundGeometry, ImageObject, HueSaturationLightnessModifier
    from comic_editor.render.pixels import working_image
    from comic_editor.render.scene import SceneSnapshotCompiler
    chapter = canvas.chapter
    chapter.pixel_contract = contract
    parent = chapter.add_layer(chapter.root_page_ids[0], 'Segment source', BoundGeometry.rectangle(0, 0, 64, 64))
    parent.fill_color, parent.border_width = None, 0
    if mapping == 'projective':
        parent.transform_frame = (0., 0., 64., 64.)
        parent.transform_quad = [(2., 3.), (62., 5.), (59., 62.), (0., 60.)]
    obj = chapter.add_object(parent.layer_id, ImageObject(x=7, y=9, pixel_width=16, pixel_height=16))
    yy, xx = np.mgrid[:16, :16]
    alpha = (.13 + (xx + 2 * yy) % 8 / 11).astype(np.float32)
    values = np.stack((xx / 17, yy / 17, ((xx + yy) % 7) / 8, np.ones_like(xx)), axis=-1).astype(np.float32)
    original = working_image(values * alpha[..., None], contract)
    canvas.images.put_decoded(obj.object_id, 'segments-native-original', b'', original)
    expected_source = native(canvas.images.native_image(obj.object_id))
    chapter.add_modifier(HueSaturationLightnessModifier(), [('object', obj.object_id)])
    canvas._publish_change_set(ChangeSet(conservative=True, label='Segment native fixture'))
    compiler = SceneSnapshotCompiler()
    snapshot = freeze(canvas, compiler, qapp)
    with ThreadPoolExecutor(max_workers=1) as worker:
        cold, state = worker.submit(exact_render, snapshot).result(timeout=20)
        warm, reused = worker.submit(exact_render, snapshot, state).result(timeout=20)
        assert reused['image_reuse_state']['nodes']
        transfer, _ = worker.submit(exact_render, snapshot, reused).result(timeout=20)
        evicted, _ = worker.submit(exact_render, snapshot, reused, evict=True).result(timeout=20)
        tiny, _ = worker.submit(exact_render, snapshot, tiny=True).result(timeout=20)
        assert cold == warm == transfer == evicted == tiny
        obj.x += 1.25
        canvas._publish_change_set(ChangeSet(conservative=True, label='Segment semantic invalidation'))
        compiler.invalidate(ChangeSet(conservative=True))
        current = freeze(canvas, compiler, qapp)
        adopted, _ = worker.submit(exact_render, current, reused).result(timeout=20)
        rebuilt, _ = worker.submit(exact_render, current).result(timeout=20)
        assert adopted == rebuilt and rebuilt != cold
    assert native(canvas.images.native_image(obj.object_id)) == expected_source
