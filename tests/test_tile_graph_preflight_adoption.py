"""Completed stage preflight takes the normal exact graph handoff."""
from collections import Counter

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage

from comic_editor.render.service import RenderPending
from comic_editor.render.tile_graph import TileCacheMiss, TileGraph, TileNode, tiles
from comic_editor.ui.async_projection import ProjectionFailed, ProjectionPending
from test_effect_job_retention import owner
from test_tile_graph_assembly import bits, finish, native_image


def forbidden(*_):
    pytest.fail('completed preflight rebuilt native source dependencies')


@pytest.mark.parametrize('shared', [False, True])
def test_completed_preflight_adopts_same_graph_key_once_across_fresh_graphs(qapp, shared):
    image = native_image(33, 29)
    frame = QRectF(7, 11, image.width(), image.height())
    completed, puts, cache = [], [], {}
    def preflight(output):
        completed.append(QRectF(output))
        return QImage(image)
    def put(scope, key, result):
        puts.append((scope, key, QImage(result)))
        cache[scope, key] = QImage(result)
    node = TileNode(frame, ('native-complete-stage',), forbidden, forbidden,
                    shared_frame=shared, cached_output=preflight)
    def graph():
        return TileGraph([TileNode(frame, ('source',), None, None), node], forbidden,
                         lambda scope, key: cache.get((scope, key)), put)
    first = graph()
    assert first.output(frame) == (image, frame)
    assert first.output(frame) == (image, frame)
    assert graph().output(frame) == (image, frame)
    expected_owner = (1, 'frame' if shared else (0, 0))
    assert len(puts) == 1 and puts[0][:2] == (
        expected_owner, ('effect-tile', node.identity, tuple(frame.getRect())))
    assert completed == [frame]


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_worker_preflight_handoff_survives_fresh_graph_and_downstream_pressure(owner, image_format):
    jobs = owner._effect_jobs
    source = native_image(73, 57, image_format)
    frame = QRectF(-13, -9, source.width(), source.height())
    context = ('source', 'chapter', 'history', 'contract')
    jobs.retained_limit = 6
    jobs.retained_budget = int(source.sizeInBytes()) * 4 + 20000
    # An older completed frame is still useful, but must yield to the demanded
    # predecessor + unfinished child when their combined admission needs space.
    # Without graph adoption the raw prefix is unprotected and evicted first.
    store = jobs.private_regions(context, lambda: context)
    older = store.begin(('older-complete-frame',),
        QRectF(0, 0, source.width() * 3, source.height()), image_format, 1)
    older.covered.add((0, 0))
    store.finish(('older-complete-frame',), older)
    calls = Counter()
    prefix_scope, prefix_key = ('native-prefix-worker',), ('native-prefix-output', context)

    def get(local_owner, key):
        entry = jobs.retained_get(('adoption-graph', local_owner), key)
        return None if entry is None else entry[0]
    def put(local_owner, key, result):
        if jobs.retained_put(('adoption-graph', local_owner), key, result, shared=True):
            jobs.retained_consume(result)
    def completed_prefix(output):
        result = jobs.result(prefix_scope, prefix_key)
        if result is not None:
            return result
        calls['prefix'] += 1
        snapshot = QImage(source)
        jobs.request(prefix_scope, prefix_key, lambda _: QImage(snapshot),
                     2 * int(snapshot.sizeInBytes()), require_exact=True)
        raise ProjectionPending(prefix_scope, prefix_key)
    def child(output, incoming, needed):
        address = (int(output.x() // 8), int(output.y() // 8))
        scope, key = ('native-child', address), ('child-output', context, address)
        result = jobs.result(scope, key)
        if result is not None:
            return result
        calls[address] += 1
        snapshot = QImage(incoming)
        crop = output.translated(-needed.topLeft()).toAlignedRect()
        jobs.request(scope, key, lambda _: snapshot.copy(crop),
                     2 * int(snapshot.sizeInBytes()), require_exact=True)
        raise ProjectionPending(scope, key)
    nodes = [TileNode(frame, ('source', context), None, None, format=image_format),
             TileNode(frame, ('complete-prefix', context), forbidden, forbidden,
                      shared_frame=True, format=image_format, cached_output=completed_prefix),
             TileNode(frame, ('child', context), lambda _: frame, child, format=image_format)]
    tile_count = len(list(tiles(frame, frame, 8)))
    for _ in range(2 * tile_count + 4):
        graph = TileGraph(nodes, forbidden, get, put, tile_size=8,
                          region_store=jobs.private_regions(context, lambda: context))
        try:
            result, actual_frame = graph.output(frame)
            break
        except RenderPending:
            finish(jobs)
        assert jobs.retained_bytes <= jobs.retained_budget
        assert len(jobs.retained) + len(jobs._regions) <= jobs.retained_limit
    else:
        pytest.fail('completed predecessor did not survive exact downstream progress')
    oracle = TileGraph([TileNode(frame, ('native-oracle',), None, None, format=image_format)],
        lambda rect: source.copy(rect.translated(-frame.topLeft()).toAlignedRect()),
        lambda *_: None, lambda *_: None, tile_size=8).output(frame)[0]
    assert actual_frame == frame and bits(result) == bits(oracle)
    assert calls['prefix'] == 1
    assert sum(value for key, value in calls.items() if key != 'prefix') == tile_count
    assert max(calls.values()) == 1
    assert ('result', prefix_scope) not in jobs.retained
    assert ('older-complete-frame',) not in jobs._regions
    assert jobs.retained_bytes <= jobs.retained_budget


@pytest.mark.parametrize('event', ['pending', 'failed', 'null'])
def test_unfinished_preflight_is_not_admitted_to_graph_cache(qapp, event):
    frame = QRectF(0, 0, 16, 16)
    writes = []
    def preflight(_):
        if event == 'pending':
            raise RenderPending('pending stage')
        if event == 'failed':
            raise ProjectionFailed('failed-stage', 'key', 'worker failed')
        return QImage()
    node = TileNode(frame, ('stage',), forbidden, forbidden, cached_output=preflight)
    graph = TileGraph([TileNode(frame, ('source',), None, None), node], forbidden,
                      lambda *_: None, lambda *args: writes.append(args), cache_only=True)
    error = {'pending': RenderPending, 'failed': ProjectionFailed, 'null': TileCacheMiss}[event]
    with pytest.raises(error):
        graph.output(frame)
    assert not writes and not graph._request_cache


def test_cache_only_completed_preflight_stays_read_only_and_has_no_private_buffer(owner):
    source = native_image(33, 29)
    frame = QRectF(0, 0, source.width(), source.height())
    jobs = owner._effect_jobs
    context = ('current',)
    node = TileNode(frame, ('completed',), forbidden, forbidden, shared_frame=True,
                    cached_output=lambda _: QImage(source))
    graph = TileGraph([TileNode(frame, ('source',), None, None), node], forbidden,
        lambda *_: None, forbidden, cache_only=True,
        region_store=jobs.private_regions(context, lambda: context))
    assert graph.region_store is None
    assert graph.output(frame) == (source, frame)
    assert not jobs.retained and not jobs._regions and not jobs.retained_bytes


@pytest.mark.parametrize('change', ['source', 'chapter', 'history', 'contract', 'cancel', 'live-cancel'])
@pytest.mark.parametrize('complete', [False, True])
def test_reentrant_preflight_context_change_cannot_adopt_or_rebuild_stale_pixels(owner, change, complete):
    jobs = owner._effect_jobs
    context = ['source', 'chapter', 'history', 'contract']
    frame, source = QRectF(0, 0, 16, 16), native_image(16, 16)
    writes = []
    def preflight(_):
        if change == 'cancel':
            jobs.cancel(clear_retained=False)
        elif change == 'live-cancel':
            jobs.cancel_exact()
        else:
            context[['source', 'chapter', 'history', 'contract'].index(change)] += '-new'
        return QImage(source) if complete else None
    node = TileNode(frame, ('old-context',), forbidden, forbidden, cached_output=preflight)
    graph = TileGraph([TileNode(frame, ('source',), None, None), node], forbidden,
        lambda *_: None, lambda *args: writes.append(args),
        region_store=jobs.private_regions(tuple(context), lambda: tuple(context)))
    with pytest.raises(RenderPending):
        graph.output(frame)
    assert not writes and not graph._request_cache
    assert not jobs.retained and not jobs._regions
