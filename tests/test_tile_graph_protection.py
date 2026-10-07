"""Demanded whole frames retain priority over completed regional/output aliases."""
from collections import Counter
from threading import Event

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage

from comic_editor.render.service import RenderPending
from comic_editor.render.tile_graph import TileGraph, TileNode, tiles
from comic_editor.ui.async_projection import ProjectionPending, projection_result_or_pending
from comic_editor.ui.tile_effects import _put_graph_result
from test_effect_job_retention import owner
from test_tile_graph_assembly import bits, finish, native_image


FORMATS = [QImage.Format_ARGB32_Premultiplied,
           QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied]


def backend(canvas, scope):
    jobs = canvas._effect_jobs
    def get(local_owner, key):
        value = jobs.retained_get(('tile-graph', scope, local_owner), key)
        return None if value is None else value[0]
    def put(local_owner, key, result):
        _put_graph_result(canvas, scope, local_owner, key, result)
    return get, put


def crop_oracle(source, frame, requested, tile_size):
    return TileGraph([TileNode(frame, ('oracle',), None, None, format=source.format())],
        lambda rect: source.copy(rect.translated(-frame.topLeft()).toAlignedRect()),
        lambda *_: None, lambda *_: None, tile_size=tile_size).output(requested)[0]


@pytest.mark.parametrize('image_format', FORMATS)
def test_large_whole_prefix_survives_many_child_adoptions_and_output_byte_pressure(owner, image_format):
    jobs = owner._effect_jobs
    source = native_image(100, 100, image_format)
    frame, context = QRectF(0, 0, 100, 100), ('chapter', 'history', 'native-contract')
    jobs.retained_budget = int(source.sizeInBytes()) * 20 // 9
    jobs.retained_limit = 512
    assert .89 < source.sizeInBytes() / (jobs.retained_budget // 2) < .91
    get, put = backend(owner, ('large-prefix-pressure',))
    calls = Counter()
    worker_scope, worker_key = ('large-prefix-worker',), ('exact-native-frame', context)
    def preflight(_):
        result = projection_result_or_pending(owner, worker_scope, worker_key)
        if result is not None:
            return result
        calls['prefix'] += 1
        snapshot = QImage(source)
        jobs.request(worker_scope, worker_key, lambda _: snapshot.copy(),
                     2 * int(snapshot.sizeInBytes()), require_exact=True)
        raise ProjectionPending(worker_scope, worker_key)
    def forbidden(*_):
        pytest.fail('completed prefix rebuilt its native upstream source')
    def child(output, incoming, needed):
        calls['child'] += 1
        return incoming.copy(output.translated(-needed.topLeft()).toAlignedRect())
    nodes = [TileNode(frame, ('source', context), None, None, format=image_format),
             TileNode(frame, ('whole-prefix', context), forbidden, forbidden,
                      shared_frame=True, format=image_format, cached_output=preflight),
             TileNode(frame, ('regional-child', context), lambda _: frame, child, format=image_format)]
    def graph():
        return TileGraph(nodes, forbidden, get, put, tile_size=8,
            region_store=jobs.private_regions(context, lambda: context))
    first, second = QRectF(0, 0, 100, 16), QRectF(0, 16, 100, 16)
    with pytest.raises(RenderPending):
        graph().output(first)
    finish(jobs)
    result, actual_frame = graph().output(first)
    assert actual_frame == first and bits(result) == bits(crop_oracle(source, frame, first, 8))
    assert calls['child'] == len(list(tiles(frame, first, 8)))
    # This separate one-use viewport image crosses the existing total byte
    # bound. Ordinary aliases/complete buffers must yield before the prefix.
    viewport = source.copy()
    put(('output', (1000, 0, 100, 100)), ('unrelated-viewport-output',), viewport)
    owner.results.clear()  # The ordinary scene LRU is not the progress proof.
    for _ in range(3):
        try:
            result, actual_frame = graph().output(second)
            break
        except RenderPending:
            finish(jobs)
    else:
        pytest.fail('second region could not consume the completed whole prefix')
    assert actual_frame == second and bits(result) == bits(crop_oracle(source, frame, second, 8))
    assert calls['prefix'] == 1
    whole_scope = ('tile-graph', ('large-prefix-pressure',), (1, 'frame'))
    assert whole_scope in jobs._retained_shared
    assert ('result', worker_scope) not in jobs.retained
    assert jobs.retained_bytes <= jobs.retained_budget
    assert jobs.retained_shared_bytes <= jobs.retained_budget // 2
    assert len(jobs.retained) + len(jobs._regions) <= jobs.retained_limit
    assert all(scope[2][1] == 'frame' for scope in jobs._retained_shared)


@pytest.mark.parametrize('image_format', FORMATS)
def test_finite_regional_prefix_hands_complete_cow_frame_to_shared_child_once(owner, image_format):
    jobs = owner._effect_jobs
    source = native_image(73, 57, image_format)
    frame, context = QRectF(-13, -9, 73, 57), ('finite-prefix', 'current-history', 'native-contract')
    jobs.retained_budget, jobs.retained_limit = 512 * 1024, 5
    get, put = backend(owner, ('finite-prefix-handoff',))
    calls = Counter()
    started, release = Event(), Event()
    worker_scope, worker_key = ('whole-child-worker',), ('whole-child-output', context)
    def capture(rect):
        return source.copy(rect.translated(-frame.topLeft()).toAlignedRect())
    def regional(output, incoming, needed):
        address = tuple(output.getRect())
        scope, key = ('finite-regional-worker', address), ('finite-output', context, address)
        ready = projection_result_or_pending(owner, scope, key)
        if ready is not None:
            return ready
        calls[address] += 1
        snapshot, crop = QImage(incoming), output.translated(-needed.topLeft()).toAlignedRect()
        jobs.request(scope, key, lambda _: snapshot.copy(crop),
                     2 * int(snapshot.sizeInBytes()), require_exact=True)
        raise ProjectionPending(scope, key)
    def preflight(_):
        return projection_result_or_pending(owner, worker_scope, worker_key)
    def whole(output, incoming, needed):
        calls['whole-child'] += 1
        snapshot = QImage(incoming)
        def compute(_):
            started.set()
            assert release.wait(5)
            return snapshot.copy()
        jobs.request(worker_scope, worker_key, compute,
                     2 * int(snapshot.sizeInBytes()), require_exact=True)
        raise ProjectionPending(worker_scope, worker_key)
    nodes = [TileNode(frame, ('source', context), None, None, format=image_format),
             TileNode(frame, ('finite-regional', context), lambda output: output, regional, format=image_format),
             TileNode(frame, ('whole-child', context), lambda _: frame, whole,
                      shared_frame=True, format=image_format, cached_output=preflight)]
    def graph():
        return TileGraph(nodes, capture, get, put, tile_size=8,
            region_store=jobs.private_regions(context, lambda: context))
    tile_count = len(list(tiles(frame, frame, 8)))
    for _ in range(tile_count + 4):
        try:
            result, actual_frame = graph().output(frame)
            break
        except RenderPending:
            if jobs.has_running(worker_scope, worker_key):
                try:
                    assert started.wait(1)
                    before = dict(calls)
                    # A waiting whole child cannot re-traverse its finite input.
                    with pytest.raises(RenderPending):
                        graph().output(frame)
                    assert dict(calls) == before
                finally:
                    release.set()
            finish(jobs)
        assert jobs.retained_bytes <= jobs.retained_budget
    else:
        pytest.fail('finite regional prefix did not make bounded progress')
    assert actual_frame == frame and bits(result) == bits(crop_oracle(source, frame, frame, 8))
    assert calls['whole-child'] == 1
    assert sum(value for key, value in calls.items() if key != 'whole-child') == tile_count
    assert max(calls.values()) == 1
    assert ('tile-graph', ('finite-prefix-handoff',), (2, 'frame')) in jobs._retained_shared
    assert all(scope[2][1] == 'frame' for scope in jobs._retained_shared)
    assert jobs.retained_bytes <= jobs.retained_budget
    assert jobs.retained_shared_bytes <= jobs.retained_budget // 2
