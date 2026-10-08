"""Native checkpoints share COW pixels, with bounded scene-owner bookkeeping."""
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import ChapterDocument
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene import EvaluatedScene, InlineResults


FORMATS = (
    QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4_Premultiplied,
)


def image(format, color=QColor(71, 103, 197, 149), width=17, height=11):
    result = QImage(width, height, format)
    result.fill(color)
    return result


def native(result):
    return (result.size(), result.format(), result.bytesPerLine(),
            bytes(result.constBits()))


def jobs(budget, limit=512):
    return InlineResults(SimpleNamespace(_persistent_render_cache=None), budget, limit)


def scene():
    return EvaluatedScene(SimpleNamespace(
        chapter=ChapterDocument(width=64, height=48), tiles=TileStore(), images=ImageStore(),
        settings=EditorSettings(), state={}, graphics_worker=None, viewport=(64, 48),
    ))


@pytest.mark.parametrize('format', FORMATS)
def test_worker_to_pipeline_alias_keeps_unrelated_native_checkpoint(format):
    first, second = image(format), image(format, QColor(29, 172, 48, 211))
    cache = jobs(first.sizeInBytes() + second.sizeInBytes())
    assert cache.retained_put(('pipeline', 'a'), 'a', first)
    assert cache.retained_put(('result', 'b'), 'b', second)
    completed = cache.result('b', 'b')
    assert completed is not None
    assert cache.retained_put(('pipeline', 'b'), 'b', completed)
    cache.retained_remove(('result', 'b'), 'b')
    assert native(cache.retained_get(('pipeline', 'a'), 'a')[0]) == native(first)
    assert native(cache.retained_get(('pipeline', 'b'), 'b')[0]) == native(second)
    assert cache.bytes == first.sizeInBytes() + second.sizeInBytes()


@pytest.mark.parametrize('format', FORMATS)
def test_native_alias_survives_source_and_return_value_cow_edits(format):
    source = image(format)
    expected = native(source)
    cache = jobs(source.sizeInBytes())
    state = {'frame': [1.25, -2.5, 17, 11]}
    assert cache.retained_put('stage', 'current', source, state)
    assert cache.retained_put('pipeline', 'current', QImage(source), state)
    result, returned_state = cache.retained_get('stage', 'current')
    source.fill(QColor('red'))
    result.fill(QColor('blue'))
    state['frame'][0] = 99
    returned_state['frame'][0] = 77
    assert native(source) != expected and native(result) != expected
    for scope in ('stage', 'pipeline'):
        retained, retained_state = cache.retained_get(scope, 'current')
        assert native(retained) == expected
        assert retained_state == {'frame': [1.25, -2.5, 17, 11]}
    cache.retained_remove('stage')
    assert cache.bytes == result.sizeInBytes()
    cache.retained_remove('pipeline')
    assert cache.bytes == 0 and not cache.retained


@pytest.mark.parametrize('format', FORMATS)
def test_equal_native_pixels_in_distinct_storage_still_consume_budget(format):
    first = image(format)
    independent = first.copy()
    assert native(first) == native(independent)
    assert first.cacheKey() != independent.cacheKey()
    cache = jobs(first.sizeInBytes())
    assert cache.retained_put('first', 'current', first)
    assert cache.retained_put('independent', 'current', independent)
    assert cache.retained_get('first', 'current') is None
    assert native(cache.retained_get('independent', 'current')[0]) == native(independent)
    assert cache.bytes == independent.sizeInBytes()


@pytest.mark.parametrize('format', FORMATS)
def test_alias_records_remain_bounded_and_get_keeps_lru_order(format):
    source = image(format)
    cache = jobs(source.sizeInBytes(), limit=3)
    for scope in ('a', 'b', 'c'):
        assert cache.retained_put(scope, 'same', source)
    assert cache.retained_get('a', 'same') is not None
    assert cache.retained_put('d', 'same', source)
    assert tuple(cache.retained) == ('c', 'a', 'd')
    assert cache.retained_get('b', 'same') is None
    assert cache.bytes == source.sizeInBytes()
    for scope in tuple(cache.retained):
        cache.retained_remove(scope)
    assert cache.bytes == 0


@pytest.mark.parametrize('format', FORMATS)
def test_replacement_releases_only_its_own_storage_reference(format):
    first, second = image(format), image(format, QColor(204, 75, 19, 223))
    cache = jobs(first.sizeInBytes() + second.sizeInBytes())
    assert cache.retained_put('stage', 'old', first)
    assert cache.retained_put('pipeline', 'old', first)
    assert cache.retained_put('stage', 'new', second)
    assert cache.retained_get('stage', 'old') is None
    cache.retained_remove('stage', 'old')
    assert native(cache.retained_get('stage', 'new')[0]) == native(second)
    assert native(cache.retained_get('pipeline', 'old')[0]) == native(first)
    assert cache.bytes == first.sizeInBytes() + second.sizeInBytes()
    cache.retained_remove('pipeline', 'old')
    assert cache.bytes == second.sizeInBytes()


@pytest.mark.parametrize('format', FORMATS)
def test_budget_eviction_recalculates_charge_after_last_alias_is_removed(format):
    first, second = image(format), image(format, QColor('green'))
    cache = jobs(first.sizeInBytes(), limit=2)
    assert cache.retained_put('a', 'old', first)
    assert cache.retained_put('b', 'old', first)
    assert cache.retained_put('a', 'new', second)
    assert cache.retained_get('b', 'old') is None
    assert tuple(cache.retained) == ('a',)
    assert native(cache.retained_get('a', 'new')[0]) == native(second)
    assert cache.bytes == second.sizeInBytes() <= cache.budget


@pytest.mark.parametrize('format', FORMATS)
def test_oversized_result_is_declined_without_growing_or_flushing_other_scopes(format):
    source = image(format)
    cache = jobs(source.sizeInBytes())
    assert cache.retained_put('keep', 'current', source)
    assert cache.retained_put('replace', 'old', source)
    large = image(format, width=34, height=22)
    before = native(large)
    assert not cache.retained_put('replace', 'new', large, shared=True, force=True)
    assert cache.retained_get('replace', 'old') is None
    assert native(cache.retained_get('keep', 'current')[0]) == native(source)
    assert cache.bytes == source.sizeInBytes() <= cache.budget
    assert native(large) == before


@pytest.mark.parametrize('format', FORMATS)
def test_scene_adoption_rebuilds_charge_from_native_images_not_old_counts(format):
    source = image(format)
    original = scene()
    original._effect_jobs.budget = source.sizeInBytes()
    assert original._effect_jobs.retained_put('stage', 'current', source, {'bounds': [1, 2]})
    assert original._effect_jobs.retained_put('pipeline', 'current', source, {'bounds': [1, 2]})
    values = original.cache_state()
    # Older snapshots charged both aliases; serialized entry charges are not
    # authoritative either. A new owner must measure current QImage storage.
    values['retained_bytes'] = 2 * source.sizeInBytes() + 123
    values['retained'] = OrderedDict(
        (scope, (key, result, state, -999))
        for scope, (key, result, state, _size) in values['retained'].items()
    )
    adopted = scene()
    adopted._effect_jobs.budget = source.sizeInBytes()
    # Field order must not make the obsolete byte charge authoritative.
    adopted.adopt_cache_state(OrderedDict(reversed(tuple(values.items()))))
    assert adopted._effect_jobs.bytes == source.sizeInBytes()
    assert len(adopted._effect_jobs.retained) == 2
    expected = native(source)
    for key, result, state, _size in values['retained'].values():
        result.fill(QColor('red'))
        state['bounds'][0] = 99
    for scope in ('stage', 'pipeline'):
        retained, state = adopted._effect_jobs.retained_get(scope, 'current')
        assert native(retained) == expected and state == {'bounds': [1, 2]}
    original._effect_jobs.retained_remove('stage')
    assert original._effect_jobs.bytes == source.sizeInBytes()
    assert adopted._effect_jobs.bytes == source.sizeInBytes()
    adopted._effect_jobs.retained_remove('stage')
    adopted._effect_jobs.retained_remove('pipeline')
    assert adopted._effect_jobs.bytes == 0
    assert original._effect_jobs.bytes == source.sizeInBytes()


@pytest.mark.parametrize('format', FORMATS)
def test_adopted_scene_state_has_independent_cow_handles(format):
    source = image(format)
    original = scene()
    assert original._effect_jobs.retained_put('pipeline', 'current', source)
    expected = native(source)
    values = original.cache_state()
    values['retained']['pipeline'][1].fill(QColor('red'))
    assert native(original._effect_jobs.retained_get('pipeline', 'current')[0]) == expected
    assert native(values['retained']['pipeline'][1]) != expected


@pytest.mark.parametrize('format', FORMATS)
def test_adoption_enforces_existing_owner_entry_and_byte_limits(format):
    source = image(format)
    oversized = image(format, width=34, height=22)
    entries = OrderedDict((scope, ('same', QImage(source), None, 0)) for scope in range(5))
    entries['large'] = ('large', oversized, None, 0)
    cache = jobs(source.sizeInBytes(), limit=2)
    cache.adopt_retained(entries)
    assert tuple(cache.retained) == (3, 4)
    assert cache.bytes == source.sizeInBytes() <= cache.budget
    assert cache.retained_get('large', 'large') is None
    assert native(cache.retained_get(4, 'same')[0]) == native(source)
    assert tuple(entries) == (0, 1, 2, 3, 4, 'large')
    cache.adopt_retained({})
    assert cache.bytes == 0 and not cache.retained


def test_inline_checkpoint_defaults_keep_existing_pool_limits():
    cache = InlineResults(SimpleNamespace(_persistent_render_cache=None))
    assert cache.budget == 64 * 1024 * 1024 and cache.limit == 512

