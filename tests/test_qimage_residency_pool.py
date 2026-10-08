"""Native ownership, node-wide pressure, transfer and exact-render controls.

Artifact only: these tests require root-controlled native execution. No timings
or native correctness result is implied by their presence or static parsing.
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
from comic_editor.render.scene import DetachedSceneBackend, EvaluatedScene, InlineResults
from test_inline_result_storage import FORMATS, image, native, scene


def roles(budget, limit=512):
    pool = QImageResidencyPool()
    source = QImageStorageCache(budget, limit, pool=pool)
    effect = QImageStorageCache(budget, limit, pool=pool)
    jobs = InlineResults(SimpleNamespace(_persistent_render_cache=None), budget, limit, pool=pool)
    return pool, source, effect, jobs


def accounting(pool, source, effect, jobs):
    role_images = [list(source.values()), list(effect.values()),
                   [entry[1] for entry in jobs.retained.values()]]
    union = {}
    for mapping, handles in zip((source, effect, jobs), role_images):
        distinct = {int(value.cacheKey()): int(value.sizeInBytes()) for value in handles}
        assert mapping.bytes == sum(distinct.values())
        union.update(distinct)
    assert pool.bytes == sum(union.values())
    assert pool.node_count == len(union)
    assert pool.alias_count == len(source) + len(effect) + len(jobs.retained)
    assert all(len(mapping) <= mapping.limit for mapping in (source, effect))
    assert len(jobs.retained) <= jobs.limit
    assert pool.bytes <= pool.budget or pool.pixel_node_count == 1


@pytest.mark.parametrize('format', FORMATS)
def test_role_borrowing_uses_existing_aggregate_and_true_aliases_once(format):
    first = image(format)
    size = first.sizeInBytes()
    pool, source, effect, jobs = roles(size)
    second, third = first.copy(), first.copy()
    assert native(first) == native(second) == native(third)
    assert len({first.cacheKey(), second.cacheKey(), third.cacheKey()}) == 3
    source.update(first=first, second=second, third=third)
    assert source.bytes == pool.bytes == pool.budget == 3 * size
    assert source.bytes > source.budget
    effect['stage'], effect['translated'] = first, first
    jobs.retained_put('pipeline', 'semantic', first, (2, [1.25, 2.5]))
    assert pool.bytes == 3 * size and pool.alias_count == 6
    accounting(pool, source, effect, jobs)


@pytest.mark.parametrize('format', FORMATS)
@pytest.mark.parametrize('edge', ['input', 'getitem', 'get', 'values', 'items', 'export', 'checkpoint'])
def test_every_borrowed_or_exported_handle_keeps_native_cow_storage_private(format, edge):
    original = image(format)
    expected, storage = native(original), original.cacheKey()
    pool, source, effect, jobs = roles(original.sizeInBytes())
    source['source'], effect['stage'] = original, original
    authored_state = {'frame': [1.25, -2.5, 17., 11.]}
    jobs.retained_put('scope', 'semantic', original, authored_state)
    borrowed = {'input': lambda: original, 'getitem': lambda: source['source'],
        'get': lambda: effect.get('stage'), 'values': lambda: next(source.values()),
        'items': lambda: next(effect.items())[1], 'export': lambda: source.export()['source'],
        'checkpoint': lambda: jobs.retained['scope'][1]}[edge]()
    borrowed.fill(QColor('red'))
    authored_state['frame'][0] = 99
    checkpoint = jobs.retained['scope']
    checkpoint[2]['frame'][0] = 77
    assert native(borrowed) != expected and borrowed.cacheKey() != storage
    assert native(source['source']) == native(effect['stage']) == expected
    assert jobs.retained_get('scope', 'semantic')[1]['frame'][0] == 1.25
    assert pool.bytes == original.sizeInBytes() and pool.node_count == 1
    accounting(pool, source, effect, jobs)


@pytest.mark.parametrize('format', FORMATS)
def test_global_lru_hit_evicts_oldest_node_and_every_cross_role_alias_atomically(format):
    first, second, third, fourth = (image(format, QColor(color))
                                  for color in ('red', 'green', 'blue', 'yellow'))
    pool, source, effect, jobs = roles(first.sizeInBytes())
    source['first'], effect['first'] = first, first
    jobs.retained_put('first-a', 'first', first)
    jobs.retained_put('first-b', 'first', first)
    source['second'], effect['third'] = second, third
    assert source.get('first') is not None  # refresh the whole storage node
    effect['fourth'] = fourth
    assert 'second' not in source and 'first' in source and 'first' in effect
    assert jobs.retained_get('first-a', 'first') is not None
    # First now becomes the oldest storage; alias removals happen together.
    effect.get('third'); effect.get('fourth')
    source['new-second'] = second
    assert 'first' not in source and 'first' not in effect
    assert not jobs.retained and jobs.bytes == 0
    accounting(pool, source, effect, jobs)


def test_scope_key_mismatch_is_miss_without_refresh_or_state_reuse(monkeypatch):
    first, second = image(FORMATS[0]), image(FORMATS[0], QColor('green'))
    pool, source, effect, jobs = roles(first.sizeInBytes())
    jobs.retained_put('scope', 'old-key', first, {'prefix': 3, 'placement': [1, 2]})
    source['later'] = second
    before = [value.cacheKey() for value in pool.storage_order()]
    misses = []
    monkeypatch.setattr('comic_editor.ui.cache_dependencies.cache_get',
        lambda owner, role, key: misses.append((role, key)))
    assert jobs.retained_get('scope', 'different-key') is None
    assert misses == [('retained', ('retained', 'scope', 'different-key'))]
    assert [value.cacheKey() for value in pool.storage_order()] == before
    jobs.retained_remove('scope', 'different-key')
    assert jobs.retained_get('scope', 'old-key')[1]['prefix'] == 3


def test_all_1536_alias_records_are_bounded_without_retiring_other_roles():
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes())
    for index in range(512):
        source[index] = original
        effect[index] = original
        jobs.retained_put(index, ('key', index), original, {'prefix': index})
    assert pool.alias_count == 1536 and pool.node_count == 1
    source[512] = original
    assert 0 not in source and 0 in effect and 0 in jobs.retained
    jobs.retained_put(512, ('key', 512), original)
    assert 0 not in jobs.retained and 0 in effect
    assert pool.alias_count == 1536 and pool.bytes == original.sizeInBytes()
    accounting(pool, source, effect, jobs)


def test_metadata_limit_reduction_removes_local_aliases_and_state_only():
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes(), limit=3)
    for scope in ('a', 'b', 'c'):
        source[scope] = original
        effect[scope] = original
        jobs.retained_put(scope, scope, original, {'prefix': scope})
    source.limit = 1
    jobs.limit = 1
    assert tuple(source) == tuple(jobs.retained) == ('c',)
    assert tuple(effect) == ('a', 'b', 'c')
    assert pool.alias_count == 5 and pool.node_count == 1
    accounting(pool, source, effect, jobs)


def test_metadata_retirement_does_not_refresh_a_node_owned_by_another_map():
    first, second = image(FORMATS[0]), image(FORMATS[0], QColor('green'))
    pool, source, effect, jobs = roles(first.sizeInBytes(), limit=2)
    source['old'], effect['alias'] = first, first
    source['second'] = second
    before = [value.cacheKey() for value in pool.storage_order()]
    source.limit = 1
    assert [value.cacheKey() for value in pool.storage_order()] == before
    assert 'old' not in source and 'alias' in effect
    accounting(pool, source, effect, jobs)


def test_invalid_checkpoint_state_cannot_leave_an_unrecorded_pool_alias():
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes())
    jobs.retained_put('scope', 'old', original, {'prefix': 2})
    with pytest.raises(TypeError, match='callback'):
        jobs.retained_put('scope', 'new', original, {'editor': lambda: None})
    assert jobs.retained_get('scope', 'old')[1] == {'prefix': 2}
    assert pool.alias_count == pool.node_count == 1
    accounting(pool, source, effect, jobs)


@pytest.mark.parametrize('format', FORMATS)
def test_replace_clear_last_alias_and_oversized_checkpoint_decline(format):
    first, second = image(format), image(format, QColor('green'))
    large = image(format, width=34, height=22)
    pool, source, effect, jobs = roles(first.sizeInBytes())
    source['source'], effect['stage'] = first, first
    jobs.retained_put('keep', 'old', first)
    jobs.retained_put('replace', 'old', first)
    assert not jobs.retained_put('replace', 'new', large, force=True, shared=True)
    assert 'replace' not in jobs.retained and 'keep' in jobs.retained
    source['source'] = second
    assert pool.bytes == first.sizeInBytes() + second.sizeInBytes()
    effect.clear()
    assert jobs.bytes == first.sizeInBytes()
    jobs.retained_remove('keep')
    assert pool.bytes == second.sizeInBytes()
    source.clear()
    assert pool.bytes == pool.node_count == pool.alias_count == 0


def test_source_effect_oversized_exception_is_one_exclusive_node_and_null_keeps_alias():
    large, small = image(FORMATS[0], width=4, height=4), image(FORMATS[0], width=1, height=1)
    pool, source, effect, jobs = roles(small.sizeInBytes())
    assert large.sizeInBytes() > pool.budget
    source['large'], effect['alias'] = large, large
    assert not source.store('large', QImage())
    assert not effect.store('alias', QImage())
    assert 'large' in source and 'alias' in effect
    assert pool.node_count == 1 and pool.bytes == large.sizeInBytes()
    checked_out = source['large']
    effect['small'] = small
    assert not source and list(effect) == ['small']
    assert native(checked_out) == native(large)
    assert pool.bytes == small.sizeInBytes()
    accounting(pool, source, effect, jobs)


@pytest.mark.parametrize('key_mode', ['same-key', 'changed-key'])
def test_null_checkpoint_replacement_preserves_original_key_and_detached_state_semantics(key_mode):
    original = image(FORMATS[0])
    pool, source, effect, jobs = roles(original.sizeInBytes())
    jobs.retained_put('scope', 'old', original, {'prefix': 1, 'frame': [1, 2]})
    next_key = 'old' if key_mode == 'same-key' else 'new'
    authored = {'prefix': 2, 'frame': [3, 4]}
    assert jobs.retained_put('scope', next_key, QImage(), authored)
    authored['frame'][0] = 99
    value, state = jobs.retained_get('scope', next_key)
    assert value.isNull() and state == {'prefix': 2, 'frame': [3, 4]}
    state['frame'][0] = 77
    assert jobs.retained['scope'][1].isNull()
    assert jobs.retained_get('scope', next_key)[1]['frame'] == [3, 4]
    assert pool.bytes == jobs.bytes == 0 and pool.alias_count == 1
    assert pool.pixel_node_count == 0
    accounting(pool, source, effect, jobs)


def test_null_checkpoint_keeps_metadata_cap_without_evicting_exclusive_pixel_storage():
    large = image(FORMATS[0], width=4, height=4)
    pool, source, effect, jobs = roles(4, limit=2)
    source['large'] = large
    assert large.sizeInBytes() > pool.budget
    for scope in ('a', 'b', 'c'):
        assert jobs.retained_put(scope, scope, QImage(), {'prefix': scope})
    assert tuple(jobs.retained) == ('b', 'c')
    assert jobs.retained_get('c', 'c')[0].isNull()
    assert native(source['large']) == native(large)
    assert pool.bytes == large.sizeInBytes() and pool.pixel_node_count == 1
    accounting(pool, source, effect, jobs)


def test_null_checkpoint_transfer_rebuilds_zero_charge_and_new_state_without_stale_pixels():
    owner, adopted = scene(), scene()
    original = image(FORMATS[0])
    owner._effect_jobs.retained_put('scope', 'same', original, {'prefix': 1})
    assert owner._effect_jobs.retained_put('scope', 'same', QImage(), {'prefix': 2, 'frame': [3, 4]})
    transfer = owner.cache_state()
    transfer['retained_bytes'] = 999999
    adopted.adopt_cache_state(transfer)
    transfer['retained']['scope'][2]['frame'][0] = 99
    owner._image_residency_pool.clear()
    value, state = adopted._effect_jobs.retained_get('scope', 'same')
    assert value.isNull() and state == {'prefix': 2, 'frame': [3, 4]}
    assert adopted._effect_jobs.bytes == adopted._image_residency_pool.bytes == 0
    assert adopted._image_residency_pool.alias_count == 1


def test_map_retirement_has_no_pool_cycle_or_lingering_contribution():
    pool = QImageResidencyPool()
    source = QImageStorageCache(64, pool=pool)
    source['source'] = image(FORMATS[0], width=4, height=4)
    reference = weakref.ref(source)
    del source
    gc.collect()
    assert reference() is None
    assert pool.bytes == pool.budget == pool.alias_count == pool.node_count == 0


def test_owner_budget_changes_trim_aggregate_and_metrics_without_next_kernel_put():
    owner = scene()
    first, second, third = (image(FORMATS[0], QColor(color), width=4, height=4)
                            for color in ('red', 'green', 'blue'))
    owner._modifier_source_cache_budget = 64
    owner._modifier_render_cache_budget = 64
    owner._effect_jobs.budget = 64
    owner._modifier_source_cache['source'] = first
    owner._modifier_render_cache['source-alias'] = first
    owner._modifier_render_cache['effect'] = second
    owner._effect_jobs.retained_put('scope', 'key', third)
    owner._modifier_source_cache_budget = 0
    assert owner._image_residency_pool.budget == 128
    assert owner._modifier_source_cache_bytes == 0
    assert 'source-alias' not in owner._modifier_render_cache
    assert owner._modifier_render_cache_bytes == owner._effect_jobs.bytes == 64
    owner._effect_jobs.budget = 0
    assert not owner._effect_jobs.retained and owner._effect_jobs.bytes == 0
    assert owner._image_residency_pool.bytes == 64


@pytest.mark.parametrize('format', FORMATS)
def test_transfer_rebuilds_actual_union_and_global_order_with_independent_old_owner(format):
    owner, adopted = scene(), scene()
    first, second, third = (image(format, QColor(color)) for color in ('red', 'green', 'blue'))
    owner._modifier_source_cache['source'] = first
    owner._modifier_render_cache['effect'] = second
    owner._effect_jobs.retained_put('scope', 'key', first, {'prefix': 2, 'frame': [1, 2]})
    owner._modifier_render_cache['third'] = third
    owner._modifier_source_cache.get('source')
    state = owner.cache_state()
    expected_order = [value.cacheKey() for value in owner._image_residency_pool.storage_order()]
    state['_modifier_source_cache_bytes'] = state['_modifier_render_cache_bytes'] = state['retained_bytes'] = -999
    adopted.adopt_cache_state(OrderedDict(reversed(tuple(state.items()))))
    assert [value.cacheKey() for value in adopted._image_residency_pool.storage_order()] == expected_order
    assert adopted._image_residency_pool.bytes == sum(value.sizeInBytes() for value in (first, second, third))
    state['retained']['scope'][2]['frame'][0] = 999
    state['_modifier_source_cache']['source'].fill(QColor('yellow'))
    owner._image_residency_pool.clear()
    assert adopted._effect_jobs.retained_get('scope', 'key')[1]['frame'] == [1, 2]
    assert native(adopted._modifier_source_cache['source']) == native(first)
    assert owner._image_residency_pool.bytes == 0
    accounting(adopted._image_residency_pool, adopted._modifier_source_cache,
               adopted._modifier_render_cache, adopted._effect_jobs)


def test_backend_close_releases_all_roles_even_when_graphics_close_fails():
    owner = scene()
    original = image(FORMATS[0])
    owner._modifier_source_cache['source'], owner._modifier_render_cache['effect'] = original, original
    owner._effect_jobs.retained_put('scope', 'key', original)
    transferred = owner.cache_state()
    class Failure:
        def close(self):
            raise RuntimeError('original close error')
    owner._gpu_pattern_renderer = Failure()
    with pytest.raises(RuntimeError, match='original close error'):
        DetachedSceneBackend.close(SimpleNamespace(scene=owner))
    assert owner._image_residency_pool.bytes == 0
    assert not owner._modifier_source_cache and not owner._modifier_render_cache and not owner._effect_jobs.retained
    assert native(transferred['_modifier_source_cache']['source']) == native(original)


from test_detached_fit_parent_source import canvas, freeze, POLICIES


def exact_render(snapshot, state=None, *, tiny=False, evict=False):
    from comic_editor.render.service import DocumentRenderService, RenderRequest, RenderQuality
    backend = DetachedSceneBackend(snapshot)
    try:
        owner = backend.scene
        if state is not None:
            owner.adopt_cache_state(state)
        if tiny:
            owner._modifier_source_cache_budget = owner._modifier_render_cache_budget = 1
            owner._effect_jobs.budget = 1
        if evict:
            pressure = image(snapshot.document.pixel_contract.image_format, width=513, height=511)
            owner._modifier_render_cache_budget = owner._modifier_source_cache_budget = (pressure.sizeInBytes() - 1) // 3
            owner._effect_jobs.budget = (pressure.sizeInBytes() - 1) // 3
            owner._modifier_render_cache['pressure'] = pressure
            # One distinct image larger than the complete allowance retires
            # every prior semantic alias. Rendering must recover ordinarily.
            assert owner._image_residency_pool.node_count == 1
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        request = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ('aggregate-native',),
            snapshot.document.revision, quality=RenderQuality.EXACT)
        result = service.render_region(snapshot.document, request)
        assert result.exact and result.image.format() == snapshot.document.pixel_contract.image_format
        accounting(owner._image_residency_pool, owner._modifier_source_cache,
                   owner._modifier_render_cache, owner._effect_jobs)
        return native(result.image), owner.cache_state()
    finally:
        backend.close()
        assert backend.scene._image_residency_pool.bytes == 0


@pytest.mark.parametrize('contract', POLICIES, ids=['rgba8', 'float16', 'float32'])
@pytest.mark.parametrize('mapping', ['ordinary', 'affine', 'projective'])
@pytest.mark.parametrize('kind', ['image', 'raster'])
def test_complete_native_cold_warm_evicted_transfer_and_tiny_progress_keep_source_bits(canvas, qapp, contract, mapping, kind):
    import numpy as np
    from comic_editor.core.changes import ChangeSet
    from comic_editor.core.models import BoundGeometry, ImageObject, RasterObject, HueSaturationLightnessModifier
    from comic_editor.render.pixels import working_image
    from comic_editor.render.scene import SceneSnapshotCompiler
    chapter = canvas.chapter
    chapter.pixel_contract = contract
    parent = chapter.add_layer(chapter.root_page_ids[0], 'Mapped source', BoundGeometry.rectangle(0, 0, 64, 64))
    parent.fill_color, parent.border_width = None, 0
    if mapping != 'ordinary':
        parent.transform_frame = (0., 0., 64., 64.)
        parent.transform_quad = ([(2., 3.), (62., 5.), (60., 62.), (0., 60.)]
            if mapping == 'affine' else [(2., 3.), (62., 5.), (59., 62.), (0., 60.)])
    obj = chapter.add_object(parent.layer_id,
        ImageObject(x=7, y=9, pixel_width=16, pixel_height=16) if kind == 'image'
        else RasterObject(x=7, y=9, tile_size=16))
    yy, xx = np.mgrid[:16, :16]
    alpha = (.13+(xx+2*yy)%8/11).astype(np.float32)
    values = np.stack((xx/17, yy/17, ((xx+yy)%7)/8, np.ones_like(xx)), axis=-1).astype(np.float32)
    values *= alpha[..., None]
    original = working_image(values, contract)
    if kind == 'image':
        canvas.images.put_decoded(obj.object_id, 'aggregate-native-original', b'', original)
        source_bits = lambda: native(canvas.images.native_image(obj.object_id))
    else:
        canvas.tiles.set_tile(obj.object_id, (0, 0), original)
        source_bits = lambda: native(canvas.tiles.tile(obj.object_id, (0, 0)))
    expected_source = source_bits()
    chapter.add_modifier(HueSaturationLightnessModifier(), [('object', obj.object_id)])
    canvas._publish_change_set(ChangeSet(conservative=True, label='Aggregate native fixture'))
    compiler = SceneSnapshotCompiler()
    snapshot = freeze(canvas, compiler, qapp)
    with ThreadPoolExecutor(max_workers=1) as worker:
        cold, transfer = worker.submit(exact_render, snapshot).result(timeout=20)
        assert transfer['_modifier_source_cache'] and transfer['_modifier_render_cache']
        warm, _ = worker.submit(exact_render, snapshot, transfer).result(timeout=20)
        rebuilt, _ = worker.submit(exact_render, snapshot, transfer, evict=True).result(timeout=20)
        tiny, _ = worker.submit(exact_render, snapshot, tiny=True).result(timeout=20)
        assert cold == warm == rebuilt == tiny
        obj.x += 1.25
        canvas._publish_change_set(ChangeSet(conservative=True, label='Aggregate key invalidation'))
        compiler.invalidate(ChangeSet(conservative=True))
        current = freeze(canvas, compiler, qapp)
        transferred_current, _ = worker.submit(exact_render, current, transfer).result(timeout=20)
        fresh_current, _ = worker.submit(exact_render, current).result(timeout=20)
        assert transferred_current == fresh_current and fresh_current != cold
    assert source_bits() == expected_source
